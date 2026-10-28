"""List the Connect IQ / developer fields of a FIT file (removing them is
MyFITContainer's job, `fit_devfields.strip`).

Garmin activities carry CIQ data fields (Stryd, SmO2/CurrHemoPct, CORE Temp …)
as **developer fields**:
  - `developer_data_id` (global msg 207): per CIQ app `application_id` (UUID) +
    `developer_data_index`.
  - `field_description` (global msg 206): per field `developer_data_index`,
    `field_definition_number`, base type, `field_name`, `units`.
The fields hang off definition messages as "developer fields" (record header bit
`0x20` → dev field count + per field {field_number, size, developer_data_index}) and
carry bytes in the data messages.

`list_dev_fields()` lists them grouped per app (incl. a sample + an "empty/constant"
heuristic to spot fields with no sensor worn). `strip()` (in MyFITContainer) cuts out
selected single fields OR whole sensors byte-exactly: the affected definition
messages (drop dev defs, adjust count/flag) + data messages (drop bytes) are
rewritten, the removed app's `field_description`/`developer_data_id` go away, header
`data_size` + CRC (+ chained chunks) are set anew; everything else stays
byte-exact. This makes the empty/wrong CIQ statistics disappear on Garmin (after a
re-upload via the replace flow).
"""
from __future__ import annotations

import io
import logging
import warnings

import fitdecode
from fitdecode import types as _ft


log = logging.getLogger("myfitcontainer.devfields")

FIELD_DESCRIPTION = 206
DEVELOPER_DATA_ID = 207
_HEUR_MAX = 1500          # sample cap per field for the "empty/constant" heuristic

# Fixed application_id of the "cellTrainer" app (in the developer_data_id message).
_CELLTRAINER_UUID = "2a563aec-721a-4790-87ed-1fa8adbf7348"

# Known sensors from the CIQ field names (lowercase substrings -> display name).
# Matched against normalized field names (lower + "_" -> space), so that both
# "core temperature" and "core_temperature" match. Abbreviated spellings
# (Train.Red: currHemoPerc/O2Hb/HHb) are included as extra tokens.
_SENSOR_HINTS = [
    ({"leg spring stiffness", "form power", "air power"}, "Stryd"),
    ({"core temperature", "core body temperature", "skin temperature",
      "heat strain index"}, "CORE body temp"),
    ({"smo2", "saturated hemoglobin", "current hemoglobin", "currhemopct",
      "total hemoglobin", "thb", "hemoglobin", "hemoperc", "hemoconc",
      "o2hb"}, "Muscle O₂ (SmO2)"),
    ({"alpha1", "dfa a1", "rra1"}, "HRV (DFA a1)"),
]

# Known CIQ apps by application_id (first 8 hex chars); only applies after the
# field name hints. FORM data field (open water with goggles pairing):
# ca3ce193-fffc-4fae-9740-8a2d4c41d196, fields form_device_serial_number,
# is_form_device_connected, location_accuracy, speed_from_15s_distance …
_UUID_NAMES = {
    "ca3ce193": "FORM Smart Swim Goggles",
    "7c75ab12": "GGlance (FreeStyle Libre)",   # CGM data field, see glucose_merge
}


def _uuid(b) -> str | None:
    # Depending on the version, fitdecode returns application_id as bytes OR as a
    # tuple/list of 16 ints — accept both.
    if isinstance(b, (tuple, list)):
        try:
            b = bytes(int(x) & 0xFF for x in b)
        except (TypeError, ValueError):
            return None
    if isinstance(b, (bytes, bytearray)) and len(b) == 16:
        h = bytes(b).hex()
        return f"{h[0:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"
    return None


def _produced_by(uuid: str | None, app_version_raw) -> str | None:
    """Return "cellTrainer {major}.{minor}.{patch} ({build})" if `uuid` is the
    cellTrainer application_id (case-insensitive); cellTrainer packs version+build as
    M_mm_pp_bbb into the uint32 application_version. No/0 version: just "cellTrainer".
    None if the UUID does not match."""
    if not uuid or uuid.lower() != _CELLTRAINER_UUID:
        return None
    v = app_version_raw if isinstance(app_version_raw, int) else 0
    if v <= 0:
        return "cellTrainer"
    major = v // 10_000_000
    minor = (v // 100_000) % 100
    patch = (v // 1_000) % 100
    build = v % 1_000
    return f"cellTrainer {major}.{minor}.{patch} ({build})"


def producer_from_dev_ids(dev_apps: list[dict]) -> str | None:
    """Derive the producer from the developer_data_id messages (as dicts
    {field_name: value}) if a known app (cellTrainer) is among them —
    e.g. "cellTrainer 1.2.3 (45)". None if none is recognized. Lets the
    activity overview show cellTrainer as the source even when the file_id
    carries a fake Garmin device (FR310XT)."""
    for d in dev_apps or []:
        pb = _produced_by(_uuid(d.get("application_id")), d.get("application_version"))
        if pb:
            return pb
    return None


def _sensor_name(field_names: set[str]) -> str | None:
    low = {n.lower().replace("_", " ") for n in field_names if n}
    for sig, name in _SENSOR_HINTS:
        if any(any(s in n for n in low) for s in sig):
            return name
    return None


# ----------------------------------------------------------------- Listing


def list_dev_fields(data: bytes) -> list[dict]:
    """Developer fields grouped per CIQ app:
    [{idx, uuid, name, fields:[{fdn, name, units, carriers, n, sample,
      looks_empty, reason}]}]. `looks_empty` = no/constant value (probably
    no sensor worn). [] if there are none / on a read error."""
    apps: dict[int, dict] = {}
    samples: dict[tuple[int, int], list] = {}
    carriers: dict[tuple[int, int], set] = {}
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            with fitdecode.FitReader(io.BytesIO(data)) as fr:
                for m in fr:
                    if not isinstance(m, fitdecode.FitDataMessage):
                        continue
                    if m.name == "developer_data_id":
                        d = {f.name: f.value for f in m.fields}
                        di = d.get("developer_data_index")
                        if di is None:
                            continue
                        a = apps.setdefault(di, {"idx": di, "uuid": None,
                                                 "app_version_raw": None, "fields": {}})
                        a["uuid"] = _uuid(d.get("application_id"))
                        a["app_version_raw"] = d.get("application_version")
                    elif m.name == "field_description":
                        d = {f.name: f.value for f in m.fields}
                        di = d.get("developer_data_index")
                        fdn = d.get("field_definition_number")
                        if di is None or fdn is None:
                            continue
                        a = apps.setdefault(di, {"idx": di, "uuid": None,
                                                 "app_version_raw": None, "fields": {}})
                        a["fields"][fdn] = {
                            "fdn": fdn,
                            "name": (d.get("field_name") or "").strip() or f"field {fdn}",
                            "units": (d.get("units") or "").strip(),
                        }
                    else:
                        for f in m.fields:
                            fld = getattr(f, "field", None)
                            if isinstance(fld, _ft.DevField):
                                key = (fld.dev_data_index, fld.def_num)
                                carriers.setdefault(key, set()).add(m.name)
                                lst = samples.setdefault(key, [])
                                if (len(lst) < _HEUR_MAX
                                        and isinstance(f.value, (int, float))):
                                    lst.append(float(f.value))
    except Exception as exc:  # noqa: BLE001
        log.warning("list_dev_fields read failed: %s", exc)
        return []

    out = []
    for di, a in sorted(apps.items()):
        fields = []
        for fdn, fd in sorted(a["fields"].items()):
            vals = samples.get((di, fdn), [])
            n = len(vals)
            if n == 0:
                empty, const, sample = True, False, None
            elif all(abs(v - vals[0]) < 1e-9 for v in vals):
                empty, const, sample = False, True, vals[0]
            else:
                empty, const, sample = False, False, vals[0]
            fields.append({
                **fd, "carriers": sorted(carriers.get((di, fdn), set())),
                "n": n, "sample": sample, "looks_empty": empty or const,
                "reason": "no data" if empty else ("constant value" if const else ""),
            })
        if not fields:
            continue
        name = _sensor_name({f["name"] for f in a["fields"].values()})
        out.append({
            "idx": di, "uuid": a["uuid"],
            "produced_by": _produced_by(a["uuid"], a.get("app_version_raw")),
            "name": (name or _UUID_NAMES.get((a["uuid"] or "")[:8])
                     or (a["uuid"][:8] if a["uuid"] else f"CIQ app {di}")),
            "fields": fields,
        })
    return out
