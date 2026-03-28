import time
import re
import random

import Vars
from Function import (
    process_recent_roll_results,
    simpleRoll,
    _get_status_message,
    claim_cooldown_remaining_seconds,
)


def _log(message: str) -> None:
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{now}] {message}")


def _format_duration(seconds: float) -> str:
    """Format duration in a human-readable way."""
    if seconds < 60:
        return f"{int(seconds)}s"
    elif seconds < 3600:
        minutes = int(seconds // 60)
        secs = int(seconds % 60)
        return f"{minutes}m {secs}s"
    else:
        hours = int(seconds // 3600)
        minutes = int((seconds % 3600) // 60)
        return f"{hours}h {minutes}m"


def _validate_startup_config() -> None:
    minute = str(Vars.repeatMinute)
    if len(minute) != 2 or not minute.isdigit() or not (0 <= int(minute) <= 59):
        raise ValueError("Vars.repeatMinute must be a 2-digit string from '00' to '59'.")

    channel_raw = str(getattr(Vars, "channelId", "")).strip()
    if not channel_raw or channel_raw.startswith("PUT_"):
        raise ValueError("Vars.channelId is not configured. Set a real channel ID in Vars.py")
    if not re.search(r"\d{17,21}", channel_raw):
        raise ValueError("Vars.channelId must contain a valid numeric Discord ID.")

    server_raw = str(getattr(Vars, "serverId", "")).strip()
    if not server_raw or server_raw.startswith("PUT_"):
        raise ValueError("Vars.serverId is not configured. Set a real server ID in Vars.py")
    if not re.search(r"\d{17,21}", server_raw):
        raise ValueError("Vars.serverId must contain a valid numeric Discord ID.")


def _next_run_text(next_run_epoch: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(next_run_epoch))


def _compute_next_hourly_run(minute_str: str, now_epoch: float) -> float:
    now_struct = time.localtime(now_epoch)
    target = time.mktime(
        (
            now_struct.tm_year,
            now_struct.tm_mon,
            now_struct.tm_mday,
            now_struct.tm_hour,
            int(minute_str),
            0,
            now_struct.tm_wday,
            now_struct.tm_yday,
            now_struct.tm_isdst,
        )
    )
    if target <= now_epoch:
        target += 3600
    return target


def _random_roll_window() -> tuple[float, float]:
    minimum = float(getattr(Vars, "minCycleDelaySeconds", 3500))
    maximum = float(getattr(Vars, "maxCycleDelaySeconds", 3900))
    if minimum <= 0 or maximum <= 0:
        raise ValueError("Vars.minCycleDelaySeconds and Vars.maxCycleDelaySeconds must be > 0.")
    if minimum > maximum:
        raise ValueError("Vars.minCycleDelaySeconds cannot be greater than Vars.maxCycleDelaySeconds.")
    minimum = max(3600.0, minimum)
    maximum = max(minimum, maximum)
    return minimum, maximum


def _compute_next_random_run(now_epoch: float) -> float:
    minimum, maximum = _random_roll_window()
    return now_epoch + random.uniform(minimum, maximum)


def _should_run_cycle() -> bool:
    remaining = claim_cooldown_remaining_seconds()
    if remaining > 0:
        _log(
            "Skipping run: claim cooldown still active for "
            f"{_format_duration(remaining)}."
        )
        return False
    return True


def main() -> None:
    _validate_startup_config()

    use_random = bool(getattr(Vars, "useRandomRollInterval", False))
    if use_random:
        min_cycle, max_cycle = _random_roll_window()
        next_run_epoch = _compute_next_random_run(time.time())
        _log(
            "Scheduler started in random mode "
            f"({_format_duration(min_cycle)}-{_format_duration(max_cycle)} between runs)."
        )
    else:
        next_run_epoch = _compute_next_hourly_run(str(Vars.repeatMinute), time.time())
        _log("Scheduler started in fixed-hour mode.")

    _log(f"Next run at {_next_run_text(next_run_epoch)}")
    _log("Bot is idling and will run automatically using the configured timing mode.")

    run_on_start = bool(getattr(Vars, "runOnStart", False))
    reset_schedule_from_now = bool(getattr(Vars, "resetScheduleFromNow", False))
    if run_on_start:
        _log("runOnStart enabled: executing one immediate roll.")
        if _should_run_cycle():
            simpleRoll()
        else:
            _log("Immediate run skipped due to active claim cooldown.")
        if reset_schedule_from_now:
            if use_random:
                next_run_epoch = _compute_next_random_run(time.time())
                _log("resetScheduleFromNow enabled: next run was randomized from now.")
            else:
                next_run_epoch = time.time() + 3600
                _log("resetScheduleFromNow enabled: next run is 60 minutes from now.")
        else:
            now = time.time()
            if next_run_epoch <= now:
                if use_random:
                    next_run_epoch = _compute_next_random_run(now)
                else:
                    next_run_epoch = _compute_next_hourly_run(str(Vars.repeatMinute), now)
        _log(f"Immediate run complete. Next run at {_next_run_text(next_run_epoch)}")

    last_heartbeat = 0.0
    heartbeat_interval_minutes = float(getattr(Vars, "heartbeatIntervalMinutes", 5))
    heartbeat_interval = heartbeat_interval_minutes * 60 if heartbeat_interval_minutes > 0 else float('inf')
    
    # Keep scanning for kakera/claims while idling between roll cycles.
    idle_scan_interval = 1.0
    last_idle_scan = 0.0

    while True:
        now = time.time()

        if now - last_idle_scan >= idle_scan_interval:
            process_recent_roll_results(quiet_if_empty=True)
            last_idle_scan = now

        if now >= next_run_epoch:
            if _should_run_cycle():
                simpleRoll()
            now = time.time()
            if use_random:
                next_run_epoch = _compute_next_random_run(now)
            else:
                next_run_epoch = _compute_next_hourly_run(str(Vars.repeatMinute), now)

        if now - last_heartbeat >= heartbeat_interval:
            idle_seconds = max(0, int(next_run_epoch - now))
            formatted_duration = _format_duration(idle_seconds)
            status = _get_status_message()
            _log(
                f"Idle heartbeat: {status} | next run in {formatted_duration} at {_next_run_text(next_run_epoch)}"
            )
            last_heartbeat = now
        time.sleep(1)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        _log("Stopped by user.")
    except Exception as error:
        _log(f"Startup failed: {error}")
        raise
