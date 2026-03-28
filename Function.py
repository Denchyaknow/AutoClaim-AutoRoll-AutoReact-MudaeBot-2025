import os
import re
import sys
import time
import random
from datetime import datetime, timezone
from typing import Dict, List, Optional

import Vars

# Allow running with vendored dependencies in ./lib.
LIB_DIR = os.path.join(os.path.dirname(__file__), "lib")
if os.path.isdir(LIB_DIR) and LIB_DIR not in sys.path:
    sys.path.insert(0, LIB_DIR)

import discum

MUDAE_APPLICATION_ID = "432610292342587392"

_ALLOWED_ROLL_COMMANDS = {"mx", "ma", "mg", "wx", "wg", "wa", "hx", "ha", "hg"}

_bot: Optional[discum.Client] = None
_cached_commands: Dict[str, dict] = {}
_processed_actions: Dict[str, float] = {}
_ACTION_CACHE_TTL_SECONDS = 900

# Claim cooldown tracking
_last_claim_time: Optional[float] = None
# Last roll time tracking
_last_roll_time: Optional[float] = None
# Last claim message timestamp for cooldown calculation
_last_claim_message_timestamp: Optional[float] = None


def _effective_last_claim_timestamp() -> Optional[float]:
    """Return the best known timestamp for the most recent successful claim."""
    if _last_claim_message_timestamp is not None:
        return _last_claim_message_timestamp
    return _last_claim_time


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


def _parse_discord_timestamp(timestamp_raw: str) -> Optional[float]:
    """Parse Discord timestamp format to epoch seconds."""
    if not timestamp_raw:
        return None
    try:
        return datetime.fromisoformat(timestamp_raw.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def _extract_timestamp_from_claim_message(text: str) -> Optional[float]:
    """
    Extract timestamp from Mudae claim message.
    Mudae typically includes timestamps in messages like:
    - "Belongs to username [timestamp]"
    - Or uses Discord's timestamp format
    """
    # Try to find Discord timestamp format: <t:timestamp> or <t:timestamp:f>
    discord_ts_pattern = r'<t:(\d+)(?::\w)?>'
    match = re.search(discord_ts_pattern, text)
    if match:
        return float(match.group(1))
    
    # Try to find [timestamp] format
    bracket_ts_pattern = r'\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]'
    match = re.search(bracket_ts_pattern, text)
    if match:
        try:
            dt = datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S")
            return dt.timestamp()
        except Exception:
            pass
    
    return None


def _get_status_message() -> str:
    """Get the current status message based on active cooldowns."""
    now = time.time()
    status_parts = []
    
    # Check claim cooldown using parsed timestamp from message
    claim_cooldown_hours = float(getattr(Vars, "claimCooldownHours", 4))
    if _last_claim_message_timestamp is not None:
        time_since_claim = now - _last_claim_message_timestamp
        claim_cooldown_seconds = claim_cooldown_hours * 3600
        if time_since_claim < claim_cooldown_seconds:
            remaining = claim_cooldown_seconds - time_since_claim
            hours_ago = int(time_since_claim // 3600)
            mins_ago = int((time_since_claim % 3600) // 60)
            status_parts.append(f"⏳ Claim: {hours_ago}h {mins_ago}m ago | {_format_duration(remaining)} left")
        else:
            # Cooldown expired, but we have a record of last claim
            hours_ago = int(time_since_claim // 3600)
            mins_ago = int((time_since_claim % 3600) // 60)
            status_parts.append(f"✅ Last claim: {hours_ago}h {mins_ago}m ago")
    
    # Check roll cooldown (if using fixed interval)
    if not getattr(Vars, "useRandomRollInterval", False):
        repeat_minute = str(getattr(Vars, "repeatMinute", "00"))
        try:
            next_minute = int(repeat_minute)
            now_struct = time.localtime(now)
            current_minute = now_struct.tm_min
            
            if current_minute >= next_minute:
                # Waiting for next hour
                minutes_until = 60 - current_minute + next_minute
            else:
                minutes_until = next_minute - current_minute
            
            if minutes_until > 0:
                status_parts.append(f"⏳ Roll: {minutes_until}m")
        except (ValueError, TypeError):
            pass
    
    if status_parts:
        return " | ".join(status_parts)
    return "✅ Active"


def _normalize_roll_command(command: str) -> str:
    command = (command or "").strip().lower().lstrip("/")
    if command not in _ALLOWED_ROLL_COMMANDS:
        raise ValueError(
            f"rollCommand must be one of {sorted(_ALLOWED_ROLL_COMMANDS)}. Received: '{command}'"
        )
    return command


def _as_text(value) -> str:
    return str(value).strip() if value is not None else ""


def _normalize_discord_id(value, var_name: str) -> str:
    raw = _as_text(value)
    if not raw or raw.startswith("PUT_"):
        raise ValueError(f"{var_name} is not configured. Set it in Vars.py")

    match = re.search(r"\d{17,21}", raw)
    if not match:
        raise ValueError(f"{var_name} must contain a valid numeric Discord ID string.")
    return match.group(0)


def _channel_id() -> str:
    return _normalize_discord_id(getattr(Vars, "channelId", ""), "Vars.channelId")


def _server_id() -> str:
    return _normalize_discord_id(getattr(Vars, "serverId", ""), "Vars.serverId")


def _validate_vars() -> None:
    token = _as_text(getattr(Vars, "token", ""))
    if not token or token.startswith("PUT_") or token in {"YOUR_TOKEN_HERE"}:
        raise ValueError("Vars.token is not configured. Set your Discord token in Vars.py")

    _channel_id()
    _server_id()

    _normalize_roll_command(Vars.rollCommand)

    if not isinstance(Vars.desiredKakeras, list):
        raise ValueError("Vars.desiredKakeras must be a list of kakera names.")

    if not isinstance(Vars.desiredSeries, list):
        raise ValueError("Vars.desiredSeries must be a list of series names.")

    minute = str(Vars.repeatMinute)
    if not re.fullmatch(r"\d{2}", minute):
        raise ValueError("Vars.repeatMinute must be a 2-digit string from '00' to '59'.")
    if int(minute) < 0 or int(minute) > 59:
        raise ValueError("Vars.repeatMinute must be between '00' and '59'.")

    command_delay = float(getattr(Vars, "commandDelaySeconds", 0.0))
    if command_delay < 0:
        raise ValueError("Vars.commandDelaySeconds must be >= 0.")

    post_roll_delay = float(getattr(Vars, "postRollCollectDelaySeconds", 4.0))
    if post_roll_delay < 0:
        raise ValueError("Vars.postRollCollectDelaySeconds must be >= 0.")

    # Handle both old ms and new seconds config
    reaction_ms = float(getattr(Vars, "reactionDelayMs", 0.0))
    if reaction_ms < 0:
        raise ValueError("Vars.reactionDelayMs must be >= 0.")

    reaction_seconds = float(getattr(Vars, "reactionDelaySeconds", reaction_ms / 1000.0))
    if reaction_seconds < 0:
        raise ValueError("Vars.reactionDelaySeconds must be >= 0.")

    roll_count = int(getattr(Vars, "rollCount", 1))
    if roll_count < 1:
        raise ValueError("Vars.rollCount must be >= 1.")


def _get_bot() -> discum.Client:
    global _bot
    if _bot is None:
        _validate_vars()
        _bot = discum.Client(token=Vars.token, log={"console": False, "file": False})
    return _bot


def _build_command_data(command: str) -> dict:
    command = _normalize_roll_command(command)

    if command in _cached_commands:
        return _cached_commands[command]

    bot = _get_bot()
    resp = bot.getSlashCommands(MUDAE_APPLICATION_ID)
    if resp.status_code != 200:
        raise RuntimeError(f"Could not fetch Mudae slash commands. HTTP {resp.status_code}")

    all_commands = resp.json()
    command_info = next((cmd for cmd in all_commands if cmd.get("name") == command), None)
    if not command_info:
        raise RuntimeError(f"Slash command '/{command}' not found for Mudae application.")

    data = {
        "name": command_info["name"],
        "id": command_info["id"],
        "type": command_info.get("type", 1),
        "version": command_info["version"],
        "options": [],
        "attachments": [],
        "application_command": command_info,
    }
    _cached_commands[command] = data
    return data


def _trigger_slash(command: str) -> None:
    data = _build_command_data(command)
    bot = _get_bot()
    response = bot.triggerSlashCommand(
        MUDAE_APPLICATION_ID,
        _channel_id(),
        guildID=_server_id(),
        data=data,
    )
    if response.status_code not in (200, 204):
        _log(f"Failed to run /{command}. HTTP {response.status_code} | {response.text[:180]}")
    else:
        _log(f"Triggered /{command}")


def _extract_message_text(message: dict) -> str:
    parts: List[str] = []
    if message.get("content"):
        parts.append(message["content"])

    for embed in message.get("embeds", []):
        for key in ("title", "description"):
            value = embed.get(key)
            if value:
                parts.append(value)
        author = embed.get("author", {}).get("name")
        if author:
            parts.append(author)
        for field in embed.get("fields", []):
            name = field.get("name")
            value = field.get("value")
            if name:
                parts.append(name)
            if value:
                parts.append(value)

    return "\n".join(parts)


def _reaction_delay_seconds() -> float:
    """Get reaction delay in seconds. Handles both old ms and new seconds config."""
    if hasattr(Vars, "reactionDelaySeconds"):
        return max(0.0, float(getattr(Vars, "reactionDelaySeconds", 0.0)))
    # Legacy: convert milliseconds to seconds if reactionDelayMs is set
    if hasattr(Vars, "reactionDelayMs"):
        return max(0.0, float(getattr(Vars, "reactionDelayMs", 0.0)) / 1000.0)
    return 0.0


def _roll_delay_seconds() -> float:
    """Get roll delay in seconds. Handles both old ms and new seconds config."""
    if hasattr(Vars, "minRollDelaySeconds") and hasattr(Vars, "maxRollDelaySeconds"):
        return random.uniform(
            float(getattr(Vars, "minRollDelaySeconds", 1.2)),
            float(getattr(Vars, "maxRollDelaySeconds", 2.8))
        )
    # Legacy: convert milliseconds to seconds if using ms config
    if hasattr(Vars, "minRollDelayMs") and hasattr(Vars, "maxRollDelayMs"):
        return random.uniform(
            float(getattr(Vars, "minRollDelayMs", 1200)) / 1000.0,
            float(getattr(Vars, "maxRollDelayMs", 2800)) / 1000.0
        )
    return random.uniform(1.2, 2.8)


def _parse_discord_timestamp(timestamp_raw: str) -> Optional[float]:
    if not timestamp_raw:
        return None
    try:
        return datetime.fromisoformat(timestamp_raw.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def _message_is_new_enough(message: dict, min_epoch_seconds: float) -> bool:
    ts = _parse_discord_timestamp(str(message.get("timestamp") or ""))
    if ts is None:
        return True
    return ts >= min_epoch_seconds


def _prune_processed_actions(now_epoch: float) -> None:
    stale_keys = [
        key for key, touched_at in _processed_actions.items()
        if now_epoch - touched_at > _ACTION_CACHE_TTL_SECONDS
    ]
    for key in stale_keys:
        _processed_actions.pop(key, None)


def _mark_action_processed(key: str) -> None:
    now_epoch = time.time()
    _prune_processed_actions(now_epoch)
    _processed_actions[key] = now_epoch


def _action_already_processed(key: str) -> bool:
    now_epoch = time.time()
    _prune_processed_actions(now_epoch)
    return key in _processed_actions


def _can_claim_now() -> bool:
    """Check if enough time has passed since the last successful claim."""
    cooldown_hours = float(getattr(Vars, "claimCooldownHours", 4))
    cooldown_seconds = cooldown_hours * 3600

    last_claim_ts = _effective_last_claim_timestamp()
    if last_claim_ts is None:
        return True

    time_since_last_claim = time.time() - last_claim_ts
    return time_since_last_claim >= cooldown_seconds


def claim_cooldown_remaining_seconds() -> float:
    """Return how many seconds remain until a claim can be made again."""
    cooldown_seconds = float(getattr(Vars, "claimCooldownHours", 4)) * 3600
    last_claim_ts = _effective_last_claim_timestamp()
    if last_claim_ts is None:
        return 0.0
    return max(0.0, cooldown_seconds - (time.time() - last_claim_ts))


def _mark_claim_successful(message: Optional[dict] = None) -> None:
    """Mark that a claim was successful, updating the last claim time."""
    global _last_claim_time, _last_claim_message_timestamp
    
    now = time.time()
    _last_claim_time = now
    
    # Try to parse timestamp from the claim message if provided
    if message is not None:
        text = _extract_message_text(message)
        parsed_timestamp = _extract_timestamp_from_claim_message(text)
        if parsed_timestamp is not None:
            _last_claim_message_timestamp = parsed_timestamp
            time_since_claim = now - parsed_timestamp
            hours_ago = int(time_since_claim // 3600)
            mins_ago = int((time_since_claim % 3600) // 60)
            _log(f"Claim timestamp parsed: {hours_ago}h {mins_ago}m ago")
        else:
            _log("Could not parse claim timestamp from message")
    
    # Print special success message
    cooldown_hours = float(getattr(Vars, "claimCooldownHours", 4))
    print("\n" + "=" * 60)
    print("🎉 CLAIM SUCCESSFUL! 🎉")
    print("=" * 60)
    print(f"✅ Claim was successfully made!")
    print(f"⏳ Next claim allowed in: {cooldown_hours:.1f} hours")
    print("=" * 60 + "\n")


def _click_component(message: dict, component: dict) -> bool:
    custom_id = component.get("custom_id")
    if not custom_id:
        return False

    bot = _get_bot()
    response = bot.click(
        applicationID=MUDAE_APPLICATION_ID,
        channelID=_channel_id(),
        messageID=message["id"],
        messageFlags=message.get("flags", 0),
        guildID=_server_id(),
        data={
            "component_type": component.get("type", 2),
            "custom_id": custom_id,
        },
    )

    if response.status_code not in (200, 204):
        _log(f"Button click failed. HTTP {response.status_code} on message {message.get('id')}")
        return False
    return True


def _iter_components(message: dict):
    for row in message.get("components", []):
        for comp in row.get("components", []):
            yield comp


def _try_react_kakera(message: dict) -> bool:
    desired = {k.strip() for k in Vars.desiredKakeras if isinstance(k, str) and k.strip()}
    if not desired:
        return False

    for component in _iter_components(message):
        emoji_name = ((component.get("emoji") or {}).get("name") or "").strip()
        if emoji_name in desired and component.get("custom_id"):
            action_key = f"kakera:{message.get('id')}:{component.get('custom_id')}"
            if _action_already_processed(action_key):
                continue
            delay_seconds = _reaction_delay_seconds()
            if delay_seconds > 0:
                time.sleep(delay_seconds)
            if _click_component(message, component):
                _mark_action_processed(action_key)
                return True
    return False


def _try_claim_series(message: dict) -> bool:
    text = _extract_message_text(message)
    desired_series = [s for s in Vars.desiredSeries if isinstance(s, str) and s.strip()]
    if not desired_series:
        return False

    if not any(series in text for series in desired_series):
        return False
    
    # Check if claim cooldown is active
    if not _can_claim_now():
        return False

    for component in _iter_components(message):
        custom_id = component.get("custom_id", "")
        label = (component.get("label") or "").lower()
        if "claim" in custom_id.lower() or "claim" in label:
            action_key = f"claim:{message.get('id')}:{custom_id or 'button'}"
            if _action_already_processed(action_key):
                continue
            delay_seconds = _reaction_delay_seconds()
            if delay_seconds > 0:
                time.sleep(delay_seconds)
            if _click_component(message, component):
                _mark_action_processed(action_key)
                # Mark claim as successful - cooldown will prevent another claim
                _mark_claim_successful(message)
                return True

    # Legacy fallback when claim is still a heart reaction.
    # Check cooldown again for legacy path
    if not _can_claim_now():
        return False
    
    action_key = f"claim:{message.get('id')}:heart"
    if _action_already_processed(action_key):
        return False
    
    bot = _get_bot()
    delay_seconds = _reaction_delay_seconds()
    if delay_seconds > 0:
        time.sleep(delay_seconds)
    heart_response = bot.addReaction(_channel_id(), message["id"], "❤")
    if heart_response.status_code in (200, 204):
        _mark_action_processed(action_key)
        # Mark claim as successful - cooldown will prevent another claim
        _mark_claim_successful(message)
        return True
    
    return False


def _load_recent_mudae_messages(limit: int = 12) -> List[dict]:
    bot = _get_bot()
    response = bot.getMessages(_channel_id(), num=limit)
    if response.status_code != 200:
        _log(f"Could not read channel messages. HTTP {response.status_code}")
        return []

    messages = response.json()
    if not isinstance(messages, list):
        return []

    return [
        m for m in messages if ((m.get("author") or {}).get("id") == MUDAE_APPLICATION_ID)
    ]


def process_recent_roll_results(
    min_timestamp: Optional[float] = None,
    quiet_if_empty: bool = True,
    limit: int = 20,
) -> tuple[bool, bool]:
    messages = _load_recent_mudae_messages(limit=limit)
    if messages and min_timestamp is not None:
        messages = [m for m in messages if _message_is_new_enough(m, min_timestamp)]

    if not messages:
        return False, False

    claimed = False
    reacted = False
    for msg in messages:
        if not reacted:
            reacted = _try_react_kakera(msg) or reacted
        if not claimed:
            claimed = _try_claim_series(msg) or claimed
        if reacted and claimed:
            break
    return claimed, reacted


def simpleRoll() -> None:
    global _last_roll_time
    
    try:
        _validate_vars()
        roll_command = _normalize_roll_command(Vars.rollCommand)
        roll_count = int(getattr(Vars, "rollCount", 1))
        command_delay = max(0.0, float(getattr(Vars, "commandDelaySeconds", 0.0)))

        _log(f"Starting run | rollCount={roll_count}")
        roll_started_at = time.time()
        
        for i in range(roll_count):
            # Trigger the roll command
            _trigger_slash(roll_command)
            
            # Wait for roll delay (randomized) before next roll
            if i < roll_count - 1:
                roll_delay = _roll_delay_seconds()
                if roll_delay > 0:
                    time.sleep(roll_delay)
            
            # After each roll, check for claims/kakera
            process_recent_roll_results(min_timestamp=roll_started_at - 1.0, quiet_if_empty=True)

        if bool(Vars.pokeRoll):
            if command_delay > 0:
                time.sleep(command_delay)
            try:
                _trigger_slash("p")
            except Exception as poke_error:
                _log(f"Pokeslot slash was skipped: {poke_error}")

        # Give Discord/Mudae time to post interaction responses.
        collect_delay = max(0.0, float(getattr(Vars, "postRollCollectDelaySeconds", 4.0)))
        if collect_delay > 0:
            time.sleep(collect_delay)

        # Final check for any remaining claims/kakera
        claimed, reacted = process_recent_roll_results(min_timestamp=roll_started_at - 1.0)

        _log(f"Run finished | claim={claimed} | kakeraReact={reacted}")
        
        # Track last roll time
        _last_roll_time = time.time()

    except Exception as error:
        _log(f"Run failed: {error}")
