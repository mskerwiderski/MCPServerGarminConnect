from garmin_mcp import fitview


def test_metrics_reads_the_session(fit_bytes):
    m = fitview.metrics(fit_bytes)
    assert m["sport"] == "running"
    assert 590 <= m["duration_s"] <= 600
    assert 1800 < m["distance_m"] < 2000
    assert m["avg_hr"] == 139
    assert "laps" not in m                      # laps belong to the stream tool


def test_streams_are_resampled_and_summarised(fit_bytes):
    view = fitview.stream_view(fit_bytes, max_points=50)
    assert view["n_points"] == 50
    assert view["n_source_points"] > 50
    assert "hr" in view["available_channels"]
    hr = view["channels"]["hr"]
    assert len(hr["values"]) == 50
    assert hr["min"] >= 120 and hr["max"] <= 160
    assert len(view["t_s"]) == 50


def test_channel_filter_and_point_cap(fit_bytes):
    view = fitview.stream_view(fit_bytes, channels=["hr"], max_points=10_000)
    assert list(view["channels"]) == ["hr"]
    assert view["n_points"] <= fitview.MAX_POINTS_CAP


def test_short_series_is_not_padded(fit_bytes):
    view = fitview.stream_view(fit_bytes, max_points=500)
    assert len(view["t_s"]) == view["n_points"] <= 500


def test_sensors_empty_for_a_file_without_ciq(fit_bytes):
    assert fitview.sensors(fit_bytes) == []


def test_two_records_per_second_are_merged_before_downsampling(tmp_path):
    """COROS PACE Pro with Stryd: a CIQ record and a native record per second.
    Without the merge the index stride sampled only every other message and
    the GPS track collapsed to a straight line."""
    from datetime import datetime, timezone
    from fit_tool.fit_file_builder import FitFileBuilder
    from fit_tool.profile.messages.file_id_message import FileIdMessage
    from fit_tool.profile.messages.record_message import RecordMessage
    from fit_tool.profile.messages.session_message import SessionMessage
    from fit_tool.profile.profile_type import FileType, Manufacturer, Sport
    from garmin_mcp.fit import streams
    b = FitFileBuilder(auto_define=True)
    fid = FileIdMessage()
    fid.type = FileType.ACTIVITY
    fid.manufacturer = Manufacturer.GARMIN.value
    base = int(datetime(2025, 9, 21, 12, 30, 0, tzinfo=timezone.utc).timestamp() * 1000)
    fid.time_created = base
    b.add(fid)
    n = 1600                                    # > 1500 points -> stride 2
    for i in range(n):
        a = RecordMessage()                     # "CIQ" record: HR only
        a.timestamp = base + i * 1000
        a.heart_rate = 140
        b.add(a)
        r = RecordMessage()                     # native record: GPS + speed
        r.timestamp = base + i * 1000
        r.position_lat = 48.30 + i * 0.00001
        r.position_long = 11.90 + i * 0.00001
        r.speed = 3.0
        r.distance = i * 3.0
        b.add(r)
    s = SessionMessage()
    s.sport = Sport.RUNNING.value
    s.start_time = base
    s.total_timer_time = float(n)
    s.total_distance = 3.0 * n
    b.add(s)
    path = tmp_path / "coros_double.fit"
    b.build().to_file(str(path))
    st = streams.extract_streams(path.read_bytes())
    assert st["n"] == 801                       # 1600 s, stride 2, plus last record
    assert len(st["gps"]) == st["n"]            # every sample carries GPS ...
    assert all(v is not None for v in st["channels"]["speed"])   # ... and speed
    assert all(v is not None for v in st["channels"]["hr"])
