"""WRF history alarm offsets shared by planning, rendering and validation."""

# WRF share/set_timekeeping.F uses this duration for an unbounded alarm.
UNBOUNDED_SECONDS = 1700000000


def history_settings(control, index, interval_minutes=60, begin_minutes=0):
    def per(name, default=0):
        value = control.get(name, default)
        if value is None:
            value = default
        return value[index] if isinstance(value, (list, tuple)) else value

    def seconds(prefix, default_minutes=0):
        days, hours, minutes, seconds = (per(prefix + '_' + unit) for unit in ('d', 'h', 'm', 's'))
        # WRF substitutes the legacy minute field only when *_m is zero,
        # including when days/hours/seconds are explicitly supplied.
        if minutes == 0:
            minutes = per(prefix, default_minutes)
        if min(days, hours, minutes, seconds) < 0:
            raise ValueError('negative_history_offset:' + prefix)
        return days * 86400 + hours * 3600 + minutes * 60 + seconds

    interval = seconds('history_interval', interval_minutes)
    begin = seconds('history_begin', begin_minutes)
    end = seconds('history_end')
    # WRF's zero history_end means run_length + padding, not model time zero.
    return interval, begin, end if end > 0 else UNBOUNDED_SECONDS
