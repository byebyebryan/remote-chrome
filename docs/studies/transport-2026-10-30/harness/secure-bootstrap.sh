set -u

chrome_command="${1:-}"
secret_application="${2:-}"
secret_schema="${3:-}"
notify_socket="${4:-}"
notify_apps="${5:-}"
shift 5 2>/dev/null || exit 64

runtime_dir="${XDG_RUNTIME_DIR:-/tmp}"
normal_bus="${DBUS_SESSION_BUS_ADDRESS:-}"
case "$normal_bus" in
  /*) normal_bus="unix:path=$normal_bus" ;;
esac
export DBUS_SESSION_BUS_ADDRESS="$normal_bus"
proxy_socket="$runtime_dir/remote-chrome-dbus-proxy-${BASHPID:-$$}.sock"
notify_forwarder_script=""
notify_forwarder_script_inode=""
notify_helper_log=""
notify_helper_log_inode=""
notify_ready_file=""
notify_ready_file_inode=""
umask 077
proxy_pid=""
proxy_starttime=""
proxy_socket_owned=0
proxy_socket_inode=""
secret_pid=""
secret_starttime=""
secret_owned=0
notify_pid=""
notify_starttime=""
notify_process_description="notify forwarder"
notify_socket_inode=""
notify_backend="disabled"
notify_capabilities=""
notify_error=""
chrome_pid=""
chrome_starttime=""
incoming_state=""
cleanup_done=0
cleanup_status=0

remote_secure_pid_stat() {
  local pid="$1"
  local stat_line rest state parent_pid starttime index
  local -a stat_fields=()
  [[ "$pid" =~ ^[0-9]+$ ]] || return 1
  IFS= read -r stat_line <"/proc/$pid/stat" 2>/dev/null || return 1
  case "$stat_line" in
    "$pid ("*) stat_line="${stat_line#"$pid ("}" ;;
    *) return 1 ;;
  esac

  # comm may contain spaces and right parentheses.  Scan candidates from the
  # end and accept only the one whose state/ppid/starttime fields are valid.
  for ((index = ${#stat_line} - 1; index >= 0; index--)); do
    [ "${stat_line:index:1}" = ")" ] || continue
    rest="${stat_line:index+1}"
    stat_fields=()
    read -r -a stat_fields <<<"$rest"
    [ "${#stat_fields[@]}" -ge 20 ] || continue
    state="${stat_fields[0]}"
    case "$state" in
      R|S|D|Z|T|t|W|X|K|P) ;;
      *) continue ;;
    esac
    parent_pid="${stat_fields[1]}"
    starttime="${stat_fields[19]}"
    [[ "$parent_pid" =~ ^[0-9]+$ ]] || continue
    [[ "$starttime" =~ ^[0-9]+$ ]] || continue
    printf '%s\t%s\n' "$starttime" "$parent_pid"
    return 0
  done
  return 1
}

remote_secure_pid_starttime() {
  local pid="$1"
  local identity starttime parent_pid
  identity="$(remote_secure_pid_stat "$pid" 2>/dev/null)" || return 1
  IFS=$'\t' read -r starttime parent_pid <<<"$identity"
  [ -n "$starttime" ] || return 1
  printf '%s\n' "$starttime"
}

remote_secure_pid_parent() {
  local pid="$1"
  local identity starttime parent_pid
  identity="$(remote_secure_pid_stat "$pid" 2>/dev/null)" || return 1
  IFS=$'\t' read -r starttime parent_pid <<<"$identity"
  [ -n "$parent_pid" ] || return 1
  printf '%s\n' "$parent_pid"
}

remote_secure_pid_matches() {
  local pid="$1"
  local recorded_starttime="$2"
  local expected="${3:-}"
  local expected_parent="${4:-}"
  local command_line first current_starttime current_parent
  [[ "$pid" =~ ^[0-9]+$ ]] || return 1
  [[ "$recorded_starttime" =~ ^[0-9]+$ ]] || return 1
  current_starttime="$(remote_secure_pid_starttime "$pid" 2>/dev/null || true)"
  [ "$current_starttime" = "$recorded_starttime" ] || return 1
  if [ -n "$expected" ]; then
    [ -r "/proc/$pid/cmdline" ] || return 1
    command_line="$(tr '\0' ' ' <"/proc/$pid/cmdline" 2>/dev/null || true)"
    first="${command_line%% *}"
    first="${first##*/}"
    [ "$first" = "$expected" ] || return 1
  fi
  if [ -n "$expected_parent" ]; then
    current_parent="$(remote_secure_pid_parent "$pid" 2>/dev/null || true)"
    [ "$current_parent" = "$expected_parent" ] || return 1
  fi
}

remote_secure_child_pid_matches() {
  local pid="$1"
  local recorded_starttime="$2"
  # Custom Chrome commands may be wrapper scripts whose /proc executable is
  # the interpreter.  The direct-child relationship plus birth identity is
  # the stable ownership check for this child; helper daemons additionally
  # require their expected executable name below.
  remote_secure_pid_matches "$pid" "$recorded_starttime" "" "$$"
}

remote_secure_stop_pid() {
  local pid="$1"
  local recorded_starttime="$2"
  local expected="$3"
  local attempt
  remote_secure_pid_matches "$pid" "$recorded_starttime" "$expected" "$$" || return 1
  kill -TERM "$pid" >/dev/null 2>&1 || true
  for ((attempt = 0; attempt < 20; attempt++)); do
    remote_secure_pid_matches "$pid" "$recorded_starttime" "$expected" "$$" || return 0
    sleep 0.1
  done
  if remote_secure_pid_matches "$pid" "$recorded_starttime" "$expected" "$$"; then
    kill -KILL "$pid" >/dev/null 2>&1 || true
  fi
  for ((attempt = 0; attempt < 20; attempt++)); do
    remote_secure_pid_matches "$pid" "$recorded_starttime" "$expected" "$$" || return 0
    sleep 0.1
  done
  return 1
}

remote_secure_stop_child_pid() {
  local pid="$1"
  local recorded_starttime="$2"
  local attempt
  remote_secure_child_pid_matches "$pid" "$recorded_starttime" || return 1
  kill -TERM "$pid" >/dev/null 2>&1 || true
  for ((attempt = 0; attempt < 20; attempt++)); do
    remote_secure_child_pid_matches "$pid" "$recorded_starttime" || return 0
    sleep 0.1
  done
  if remote_secure_child_pid_matches "$pid" "$recorded_starttime"; then
    kill -KILL "$pid" >/dev/null 2>&1 || true
  fi
  for ((attempt = 0; attempt < 20; attempt++)); do
    remote_secure_child_pid_matches "$pid" "$recorded_starttime" || return 0
    sleep 0.1
  done
  return 1
}

remote_secure_wait_owned_pid() {
  local pid="$1"
  local recorded_starttime="$2"
  local expected="$3"
  local attempt
  for ((attempt = 0; attempt < 20; attempt++)); do
    remote_secure_pid_matches "$pid" "$recorded_starttime" "$expected" "$$" && return 0
    kill -0 "$pid" >/dev/null 2>&1 || return 1
    sleep 0.1
  done
  remote_secure_pid_matches "$pid" "$recorded_starttime" "$expected" "$$"
}

remote_secure_wait_owned_child() {
  local pid="$1"
  local recorded_starttime="$2"
  local attempt
  for ((attempt = 0; attempt < 20; attempt++)); do
    remote_secure_child_pid_matches "$pid" "$recorded_starttime" && return 0
    kill -0 "$pid" >/dev/null 2>&1 || return 1
    sleep 0.1
  done
  remote_secure_child_pid_matches "$pid" "$recorded_starttime"
}

remote_secure_stop_notify_process() {
  local process_stopped=0
  [ -n "$notify_pid" ] || return 0
  if [ -z "$notify_starttime" ]; then
    echo "remote-chrome secure session: $notify_process_description PID $notify_pid has no recorded starttime; preserving it." >&2
    return 1
  elif remote_secure_wait_owned_pid "$notify_pid" "$notify_starttime" python3; then
    if remote_secure_stop_pid "$notify_pid" "$notify_starttime" python3; then
      process_stopped=1
    else
      echo "remote-chrome secure session: owned $notify_process_description PID $notify_pid remains after TERM/KILL; inspect it before retrying." >&2
      return 1
    fi
  elif kill -0 "$notify_pid" >/dev/null 2>&1; then
    echo "remote-chrome secure session: $notify_process_description PID $notify_pid identity changed; preserving the replacement process." >&2
    return 1
  else
    process_stopped=1
  fi
  if [ "$process_stopped" = "1" ]; then
    wait "$notify_pid" >/dev/null 2>&1 || true
    notify_pid=""
    notify_starttime=""
  fi
}

remote_secure_remove_notify_file() {
  local path="$1"
  local expected_inode="$2"
  local description="$3"
  local current_inode
  [ -n "$path" ] || return 0
  if [ ! -e "$path" ] && [ ! -L "$path" ]; then
    return 0
  fi
  if [ -L "$path" ] || [ ! -f "$path" ] || [ ! -O "$path" ]; then
    echo "remote-chrome secure session: $description identity changed; preserving $path." >&2
    return 1
  fi
  current_inode="$(stat -c '%d:%i' "$path" 2>/dev/null || true)"
  if [ -z "$expected_inode" ] || [ "$current_inode" != "$expected_inode" ]; then
    echo "remote-chrome secure session: $description identity changed; preserving $path." >&2
    return 1
  fi
  if ! rm -f -- "$path" || [ -e "$path" ] || [ -L "$path" ]; then
    echo "remote-chrome secure session: could not remove owned $description $path." >&2
    return 1
  fi
}

remote_secure_cleanup() {
  [ "$cleanup_done" = "0" ] || return "$cleanup_status"
  # Protect both normal Chrome exit and EXIT-trap cleanup from teardown signals.
  # Finish the bounded ledger before accepting another termination.
  trap '' INT TERM HUP
  cleanup_done=1
  local cleanup_failed=0
  local process_stopped=0

  if [ -n "$chrome_pid" ]; then
    process_stopped=0
    if [ -z "$chrome_starttime" ]; then
      echo "remote-chrome secure session: Chrome child PID $chrome_pid has no recorded starttime; preserving it." >&2
      cleanup_failed=1
    elif remote_secure_wait_owned_child "$chrome_pid" "$chrome_starttime"; then
      if remote_secure_stop_child_pid "$chrome_pid" "$chrome_starttime"; then
        process_stopped=1
      else
        echo "remote-chrome secure session: owned Chrome child PID $chrome_pid remains after TERM/KILL; inspect it before retrying." >&2
        cleanup_failed=1
      fi
    elif kill -0 "$chrome_pid" >/dev/null 2>&1; then
      echo "remote-chrome secure session: Chrome child PID $chrome_pid identity changed; preserving the replacement process." >&2
      cleanup_failed=1
    fi
    if [ "$process_stopped" = "1" ]; then
      wait "$chrome_pid" >/dev/null 2>&1 || true
    fi
  fi

  if [ -n "$proxy_pid" ]; then
    process_stopped=0
    if [ -z "$proxy_starttime" ]; then
      echo "remote-chrome secure session: proxy PID $proxy_pid has no recorded starttime; preserving it." >&2
      cleanup_failed=1
    elif remote_secure_wait_owned_pid "$proxy_pid" "$proxy_starttime" xdg-dbus-proxy; then
      if remote_secure_stop_pid "$proxy_pid" "$proxy_starttime" xdg-dbus-proxy; then
        process_stopped=1
      else
        echo "remote-chrome secure session: owned xdg-dbus-proxy PID $proxy_pid remains after TERM/KILL; inspect it before retrying." >&2
        cleanup_failed=1
      fi
    elif kill -0 "$proxy_pid" >/dev/null 2>&1; then
      echo "remote-chrome secure session: xdg-dbus-proxy PID $proxy_pid identity changed; preserving the replacement process." >&2
      cleanup_failed=1
    fi
    if [ "$process_stopped" = "1" ]; then
      wait "$proxy_pid" >/dev/null 2>&1 || true
    fi
  fi

  remote_secure_stop_notify_process || cleanup_failed=1

  remote_secure_remove_notify_file "$notify_forwarder_script" \
    "$notify_forwarder_script_inode" "notification helper script" || cleanup_failed=1
  remote_secure_remove_notify_file "$notify_ready_file" \
    "$notify_ready_file_inode" "notification readiness file" || cleanup_failed=1
  remote_secure_remove_notify_file "$notify_helper_log" \
    "$notify_helper_log_inode" "notification helper log" || cleanup_failed=1

  if [ -n "$notify_socket_inode" ] && [ -S "$notify_socket" ]; then
    current_inode="$(stat -c '%d:%i' "$notify_socket" 2>/dev/null || true)"
    if [ -z "$current_inode" ]; then
      echo "remote-chrome secure session: could not inspect notify socket $notify_socket; preserving it." >&2
      cleanup_failed=1
    elif [ "$current_inode" != "$notify_socket_inode" ]; then
      echo "remote-chrome secure session: notify socket identity changed; preserving $notify_socket." >&2
      cleanup_failed=1
    elif ! rm -f -- "$notify_socket"; then
      echo "remote-chrome secure session: could not remove notify socket $notify_socket; inspect it before retrying." >&2
      cleanup_failed=1
    fi
  fi

  if [ -n "$secret_pid" ] && [ "$secret_owned" = "1" ]; then
    process_stopped=0
    if [ -z "$secret_starttime" ]; then
      echo "remote-chrome secure session: owned ksecretd PID $secret_pid has no recorded starttime; preserving it." >&2
      cleanup_failed=1
    elif remote_secure_wait_owned_pid "$secret_pid" "$secret_starttime" ksecretd; then
      if remote_secure_stop_pid "$secret_pid" "$secret_starttime" ksecretd; then
        process_stopped=1
      else
        echo "remote-chrome secure session: owned ksecretd PID $secret_pid remains after TERM/KILL; inspect it before retrying." >&2
        cleanup_failed=1
      fi
    elif kill -0 "$secret_pid" >/dev/null 2>&1; then
      echo "remote-chrome secure session: ksecretd PID $secret_pid identity changed; preserving the replacement process." >&2
      cleanup_failed=1
    fi
    if [ "$process_stopped" = "1" ]; then
      wait "$secret_pid" >/dev/null 2>&1 || true
    fi
  fi

  if [ "$proxy_socket_owned" = "1" ]; then
    if [ -e "$proxy_socket" ] || [ -L "$proxy_socket" ]; then
      current_inode="$(stat -c '%d:%i' "$proxy_socket" 2>/dev/null || true)"
    else
      current_inode=""
    fi
    if [ -z "$current_inode" ]; then
      if [ -e "$proxy_socket" ] || [ -L "$proxy_socket" ]; then
        echo "remote-chrome secure session: could not inspect owned proxy socket $proxy_socket; preserving it." >&2
        cleanup_failed=1
      fi
    elif [ -z "$proxy_socket_inode" ]; then
      echo "remote-chrome secure session: owned proxy socket $proxy_socket has no recorded inode; preserving it." >&2
      cleanup_failed=1
    elif [ "$current_inode" != "$proxy_socket_inode" ]; then
      echo "remote-chrome secure session: proxy socket identity changed; preserving $proxy_socket." >&2
      cleanup_failed=1
    else
      if ! rm -f -- "$proxy_socket"; then
        echo "remote-chrome secure session: could not remove owned proxy socket $proxy_socket; inspect it before retrying." >&2
        cleanup_failed=1
      elif [ -e "$proxy_socket" ] || [ -L "$proxy_socket" ]; then
        echo "remote-chrome secure session: owned proxy socket $proxy_socket remains after removal." >&2
        cleanup_failed=1
      fi
    fi
  fi
  cleanup_status="$cleanup_failed"
  [ -z "$incoming_state" ] || rm -f -- "$incoming_state" || true
  return "$cleanup_status"
}

remote_secure_on_exit() {
  local exit_status="$?"
  local final_status="$exit_status"
  remote_secure_cleanup || true
  if [ "$final_status" -eq 0 ] && [ "$cleanup_status" -ne 0 ]; then
    final_status="$cleanup_status"
  fi
  trap - EXIT
  exit "$final_status"
}

remote_secure_signal() {
  local status="$1"
  trap '' INT TERM HUP
  exit "$status"
}

remote_secure_fail() {
  echo "remote-chrome secure session: $*" >&2
  exit 1
}

trap 'remote_secure_on_exit' EXIT
trap 'remote_secure_signal 130' INT
trap 'remote_secure_signal 143' TERM
trap 'remote_secure_signal 129' HUP

[ -n "$chrome_command" ] || remote_secure_fail "Chrome command is missing."
[ -n "$secret_application" ] && [ -n "$secret_schema" ] ||
  remote_secure_fail "Safe Storage identity mapping is missing."
[ -n "$normal_bus" ] ||
  remote_secure_fail "DBUS_SESSION_BUS_ADDRESS is not set to the normal remote user session bus."
[ -d "$runtime_dir" ] ||
  remote_secure_fail "XDG_RUNTIME_DIR does not exist: $runtime_dir"

for required_command in bash busctl secret-tool xdg-dbus-proxy ksecretd; do
  command -v "$required_command" >/dev/null 2>&1 ||
    remote_secure_fail "remote prerequisite is missing: $required_command"
done
command -v "$chrome_command" >/dev/null 2>&1 ||
  remote_secure_fail "remote Chrome command is missing: $chrome_command"

if [ -e "$proxy_socket" ] || [ -L "$proxy_socket" ]; then
  remote_secure_fail "refusing to reuse an existing proxy socket: $proxy_socket"
fi

secret_owner=0
if busctl --user --no-pager --quiet status org.freedesktop.secrets >/dev/null 2>&1; then
  secret_owner=1
else
  # No Secret Service owner exists. This process is started inside the
  # Waypipe environment and is the only Secret Service process this wrapper
  # may clean up. A pre-existing owner is never stopped.
  ksecretd >/dev/null 2>&1 &
  secret_pid=$!
  secret_owned=1
  secret_starttime="$(remote_secure_pid_starttime "$secret_pid" 2>/dev/null || true)"
  [ -n "$secret_starttime" ] ||
    remote_secure_fail "could not record the owned ksecretd PID $secret_pid starttime; refusing unverified cleanup."
  for ((attempt = 0; attempt < 100; attempt++)); do
    if busctl --user --no-pager --quiet status org.freedesktop.secrets >/dev/null 2>&1; then
      secret_owner=1
      break
    fi
    kill -0 "$secret_pid" >/dev/null 2>&1 ||
      remote_secure_fail "ksecretd exited before owning org.freedesktop.secrets."
    sleep 0.1
  done
fi
[ "$secret_owner" = "1" ] ||
  remote_secure_fail "no org.freedesktop.secrets owner became available; a KWallet prompt may have been canceled."

# Record only this bootstrap, not arbitrary SSH connections. This advisory
# record lets a local stop request the originating session's normal cleanup.
# Failed tracking never blocks a browser launch.
if [ -n "${REMOTE_CHROME_ORIGIN_TARGET:-}" ]; then
  incoming_pid="${BASHPID:-$$}"
  incoming_starttime="$(remote_secure_pid_starttime "$incoming_pid" 2>/dev/null || true)"
  incoming_peer="${SSH_CONNECTION:-}"
  incoming_peer="${incoming_peer%% *}"
  incoming_peer="${incoming_peer:-${REMOTE_CHROME_ORIGIN_NAME:-}}"
  if [ -n "$incoming_starttime" ]; then
    incoming_pending="$(mktemp "$runtime_dir/remote-chrome-incoming-XXXXXXXX.tmp" 2>/dev/null || true)"
    if [ -n "$incoming_pending" ]; then
      incoming_state="${incoming_pending%.tmp}.state"
      if ! {
        printf 'pid\t%s\n' "$incoming_pid"
        printf 'starttime\t%s\n' "$incoming_starttime"
        printf 'boot_id\t%s\n' "$(cat /proc/sys/kernel/random/boot_id)"
        printf 'peer\t%s\n' "$incoming_peer"
        printf 'name\t%s\n' "${REMOTE_CHROME_ORIGIN_NAME:-}"
        printf 'user\t%s\n' "${REMOTE_CHROME_ORIGIN_USER:-}"
        printf 'session\t%s\n' "${REMOTE_CHROME_ORIGIN_SESSION:-}"
        printf 'target\t%s\n' "$REMOTE_CHROME_ORIGIN_TARGET"
      } >"$incoming_pending" || ! mv -f -- "$incoming_pending" "$incoming_state"; then
        rm -f -- "$incoming_state" "$incoming_pending" || true
        incoming_state=""
        echo "remote-chrome: incoming session tracking unavailable; local stop will not discover this session." >&2
      fi
    fi
  fi
fi

# --filter with these --talk rules is intentional: Chrome needs the Secret
# Service for the password store and org.freedesktop.Notifications so web
# notifications reach the remote daemon instead of Chrome drawing its own
# toplevels (which a tiling compositor will tile on the local machine).
# org.freedesktop.portal.Desktop receives no talk/see/own policy and remains
# hidden, so GTK file choosers use Waypipe.
xdg-dbus-proxy "$normal_bus" "$proxy_socket" \
  --filter --talk=org.freedesktop.secrets --talk=org.freedesktop.Notifications >/dev/null 2>&1 &
proxy_pid=$!
proxy_socket_owned=1
proxy_starttime="$(remote_secure_pid_starttime "$proxy_pid" 2>/dev/null || true)"
[ -n "$proxy_starttime" ] ||
  remote_secure_fail "could not record the owned xdg-dbus-proxy PID $proxy_pid starttime; refusing unverified cleanup."
for ((attempt = 0; attempt < 100; attempt++)); do
  if [ -S "$proxy_socket" ]; then
    proxy_socket_inode="$(stat -c '%d:%i' "$proxy_socket" 2>/dev/null || true)"
    break
  fi
  kill -0 "$proxy_pid" >/dev/null 2>&1 ||
    remote_secure_fail "xdg-dbus-proxy exited before creating its socket."
  sleep 0.1
done
[ -n "$proxy_socket_inode" ] ||
  remote_secure_fail "xdg-dbus-proxy did not create a socket: $proxy_socket"

remote_notify_name_has_owner() {
  busctl --user --timeout=0.35 --json=short call org.freedesktop.DBus /org/freedesktop/DBus \
    org.freedesktop.DBus NameHasOwner s org.freedesktop.Notifications 2>/dev/null |
    python3 -c 'import json,sys; print("1" if json.load(sys.stdin)["data"][0] else "0")'
}

remote_notify_name_owner() {
  busctl --user --timeout=0.35 --json=short call org.freedesktop.DBus /org/freedesktop/DBus \
    org.freedesktop.DBus GetNameOwner s org.freedesktop.Notifications 2>/dev/null |
    python3 -c 'import json,sys; print(json.load(sys.stdin)["data"][0])'
}

remote_notify_owner_information() {
  local owner="$1"
  busctl --user --timeout=0.35 --json=short call "$owner" /org/freedesktop/Notifications \
    org.freedesktop.Notifications GetServerInformation 2>/dev/null |
    python3 -c 'import json,sys; d=json.load(sys.stdin)["data"]; print(str(d[0])+"\t"+str(d[1]))'
}

remote_notify_owner_capabilities() {
  local owner="$1"
  busctl --user --timeout=0.35 --json=short call "$owner" /org/freedesktop/Notifications \
    org.freedesktop.Notifications GetCapabilities 2>/dev/null |
    python3 -c 'import json,sys; d=json.load(sys.stdin)["data"]; print(",".join(str(x) for x in d[0]))'
}

remote_notify_record_process() {
  notify_pid=$!
  notify_starttime="$(remote_secure_pid_starttime "$notify_pid" 2>/dev/null || true)"
  if [ -z "$notify_starttime" ]; then
    if kill -0 "$notify_pid" >/dev/null 2>&1; then
      remote_secure_fail "could not record the owned notification helper PID $notify_pid starttime; refusing unverified cleanup."
    fi
    wait "$notify_pid" >/dev/null 2>&1 || true
    notify_pid=""
  fi
}

if [ -n "$notify_socket" ]; then
  if [ -S "$notify_socket" ]; then
    notify_socket_inode="$(stat -c '%d:%i' "$notify_socket" 2>/dev/null || true)"
  fi
  notify_forwarder_script="$runtime_dir/remote-chrome-notify-forwarder-$$.py"
  notify_helper_log="$runtime_dir/remote-chrome-notify-helper-$$.log"
  notify_ready_file="$runtime_dir/remote-chrome-notify-status-$$.state"
  for notify_candidate in "$notify_forwarder_script" "$notify_helper_log" "$notify_ready_file"; do
    if [ -e "$notify_candidate" ] || [ -L "$notify_candidate" ]; then
      notify_forwarder_script=""
      notify_helper_log=""
      notify_ready_file=""
      remote_secure_fail "refusing to overwrite an existing notification artifact: $notify_candidate"
    fi
  done
  : >"$notify_helper_log"
  chmod 600 -- "$notify_helper_log"
  notify_helper_log_inode="$(stat -c '%d:%i' "$notify_helper_log" 2>/dev/null || true)"
  source_owner_has="unknown"
  source_owner=""
  source_server=""
  source_vendor=""
  if command -v busctl >/dev/null 2>&1 && command -v python3 >/dev/null 2>&1; then
    source_owner_has="$(remote_notify_name_has_owner 2>/dev/null || true)"
  fi
  if [ "$source_owner_has" = "1" ]; then
    source_owner="$(remote_notify_name_owner 2>/dev/null || true)"
    if [ -n "$source_owner" ]; then
      owner_info="$(remote_notify_owner_information "$source_owner" 2>/dev/null || true)"
      IFS=$'\t' read -r source_server source_vendor <<<"$owner_info"
    fi
    if [ "$source_server" = "remote-chrome-notification-endpoint" ] &&
       [ "$source_vendor" = "remote-chrome" ]; then
      remote_secure_fail "another managed headless notification session owns org.freedesktop.Notifications; refusing a competing notifications-enabled Chrome session."
    elif [ -n "$source_owner" ] && [ -n "$source_server" ] && [ -n "$source_vendor" ]; then
      source_capabilities="$(remote_notify_owner_capabilities "$source_owner" 2>/dev/null || true)"
      if [[ ",$source_capabilities," == *,body,* && ",$source_capabilities," == *,actions,* ]]; then
        notify_backend="desktop"
        notify_capabilities="informational"
      else
        notify_backend="unavailable"
        notify_error="existing notification owner lacks compatible body/actions support"
        echo "remote-chrome: notifications unavailable ($notify_error); Chrome will start securely without notification forwarding." >&2
      fi
    else
      notify_backend="unavailable"
      notify_error="could not identify current notification owner"
      echo "remote-chrome: notifications unavailable ($notify_error); Chrome will start securely without notification forwarding." >&2
    fi
  elif [ "$source_owner_has" = "0" ]; then
    if [ -S "$notify_socket" ] && command -v python3 >/dev/null 2>&1; then
      cat >"$notify_forwarder_script" <<'REMOTE_CHROME_NOTIFY_ENDPOINT'
import json
import os
import secrets
import select
import signal
import socket
import sys
import time

NAME = "org.freedesktop.Notifications"
PATH = "/org/freedesktop/Notifications"
MANAGED_SERVER = "remote-chrome-notification-endpoint"
MANAGED_VENDOR = "remote-chrome"
MAX_LINE = 64 * 1024
MAX_QUEUE = 64
MAX_QUEUE_BYTES = 1 << 20
MAX_ACTIVE = 256
PROTOCOL_VERSION = 1


def write_status(path, value):
    try:
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(value + "\n")
            handle.flush()
            os.fsync(handle.fileno())
    except OSError:
        pass


def send_frame(channel, record):
    blob = (json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    if len(blob) > MAX_LINE:
        raise ValueError("oversized frame")
    channel.sendall(blob)


def read_frame(channel, deadline):
    buffer = bytearray()
    while time.monotonic() < deadline and len(buffer) <= MAX_LINE:
        ready, _, _ = select.select([channel], [], [], max(0, deadline - time.monotonic()))
        if not ready:
            break
        chunk = channel.recv(65536)
        if not chunk:
            break
        buffer.extend(chunk)
        if b"\n" in buffer:
            line, _, _rest = buffer.partition(b"\n")
            return json.loads(line)
    raise TimeoutError("notification listener handshake timed out")


class NotificationEndpoint:
    def __init__(self, dbus, bus, channel, session_id, generation, capabilities, apps):
        import dbus.service
        from gi.repository import GLib
        self.dbus = dbus
        self.bus = bus
        self.channel = channel
        self.session_id = session_id
        self.generation = generation
        self.apps = None if apps.strip() == "*" else tuple(term.lower() for term in apps.split(",") if term)
        self.capabilities = set(capabilities) & {"body", "actions", "body-markup"}
        self.GLib = GLib
        self.loop = GLib.MainLoop()
        self.input = bytearray()
        self.output = []
        self.output_bytes = 0
        self.write_watch = 0
        self.closed = False
        self.next_id = 1
        self.active = {}
        parent = self

        class EndpointObject(dbus.service.Object):
            @dbus.service.method(NAME, in_signature="", out_signature="as")
            def GetCapabilities(self):
                return dbus.Array(sorted(parent.capabilities), signature="s")

            @dbus.service.method(NAME, in_signature="", out_signature="ssss")
            def GetServerInformation(self):
                return (MANAGED_SERVER, MANAGED_VENDOR, "1", "1.2")

            @dbus.service.method(NAME, in_signature="susssasa{sv}i", out_signature="u")
            def Notify(self, app_name, replaces_id, icon, summary, body, actions, hints, expire_timeout):
                return parent.notify(app_name, replaces_id, summary, body, actions, hints, expire_timeout)

            @dbus.service.method(NAME, in_signature="u", out_signature="")
            def CloseNotification(self, notification_id):
                parent.close(notification_id)

            @dbus.service.signal(NAME, signature="us")
            def ActionInvoked(self, notification_id, key):
                pass

            @dbus.service.signal(NAME, signature="uu")
            def NotificationClosed(self, notification_id, reason):
                pass

        self.object = EndpointObject(bus, PATH)
        self.read_watch = GLib.io_add_watch(
            channel, GLib.IO_IN | GLib.IO_HUP | GLib.IO_ERR, self.on_channel
        )

    @staticmethod
    def valid_id(value):
        return isinstance(value, int) and not isinstance(value, bool) and 0 < value <= 0xFFFFFFFF

    def envelope(self, kind, **fields):
        return {"v": PROTOCOL_VERSION, "type": kind, "session_id": self.session_id, "generation": self.generation, **fields}

    def queue(self, record):
        if self.closed:
            raise self.dbus.DBusException("notification transport is unavailable")
        blob = (json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        if len(blob) > MAX_LINE or len(self.output) >= MAX_QUEUE or self.output_bytes + len(blob) > MAX_QUEUE_BYTES:
            self.disconnect("outbound-queue-limit")
            raise self.dbus.DBusException("notification transport queue is full")
        self.output.append(memoryview(blob))
        self.output_bytes += len(blob)
        if not self.write_watch:
            self.write_watch = self.GLib.io_add_watch(
                self.channel, self.GLib.IO_OUT | self.GLib.IO_HUP | self.GLib.IO_ERR, self.on_write
            )

    def on_write(self, _source, condition):
        if condition & (self.GLib.IO_HUP | self.GLib.IO_ERR):
            self.disconnect("listener-disconnected")
            return self.GLib.SOURCE_REMOVE
        while self.output:
            try:
                written = self.channel.send(self.output[0])
            except BlockingIOError:
                return self.GLib.SOURCE_CONTINUE
            except OSError:
                self.disconnect("listener-disconnected")
                return self.GLib.SOURCE_REMOVE
            if written <= 0:
                self.disconnect("listener-disconnected")
                return self.GLib.SOURCE_REMOVE
            self.output_bytes -= written
            self.output[0] = self.output[0][written:]
            if not self.output[0]:
                self.output.pop(0)
        self.write_watch = 0
        return self.GLib.SOURCE_REMOVE

    def on_channel(self, _source, condition):
        if condition & (self.GLib.IO_IN | self.GLib.IO_HUP):
            while True:
                try:
                    chunk = self.channel.recv(65536)
                except BlockingIOError:
                    break
                except OSError:
                    self.disconnect("listener-read-failed")
                    return self.GLib.SOURCE_REMOVE
                if not chunk:
                    self.disconnect("listener-disconnected")
                    return self.GLib.SOURCE_REMOVE
                self.input.extend(chunk)
                while b"\n" in self.input:
                    line, _, rest = self.input.partition(b"\n")
                    self.input = bytearray(rest)
                    if len(line) > MAX_LINE:
                        self.disconnect("oversized-frame")
                        return self.GLib.SOURCE_REMOVE
                    try:
                        self.handle(json.loads(line))
                    except (ValueError, TypeError, RecursionError):
                        continue
                if len(self.input) > MAX_LINE:
                    self.disconnect("oversized-frame")
                    return self.GLib.SOURCE_REMOVE
        if condition & (self.GLib.IO_HUP | self.GLib.IO_ERR):
            self.disconnect("listener-disconnected")
            return self.GLib.SOURCE_REMOVE
        return self.GLib.SOURCE_CONTINUE

    def handle(self, record):
        if (not isinstance(record, dict) or type(record.get("v")) is not int
                or record["v"] != PROTOCOL_VERSION or record.get("session_id") != self.session_id
                or record.get("generation") != self.generation):
            return
        kind = record.get("type")
        source_id = record.get("source_id")
        if not self.valid_id(source_id):
            return
        state = self.active.get(source_id)
        if (state is None or not self.valid_id(record.get("revision"))
                or record["revision"] != state["revision"]):
            return
        if kind == "mapped":
            local_id = record.get("local_id")
            if state is not None and self.valid_id(local_id):
                state["local_id"] = local_id
                state["mapped_revision"] = state["revision"]
                self.active[source_id] = state
        elif kind == "action":
            local_id = record.get("local_id")
            key = record.get("key")
            if (state is not None and not state["close_requested"] and self.valid_id(local_id)
                    and state["local_id"] == local_id and isinstance(key, str) and key in state["actions"]):
                self.object.ActionInvoked(self.dbus.UInt32(source_id), self.dbus.String(key))
        elif kind == "closed":
            local_id = record.get("local_id")
            reason = record.get("reason")
            if (state is not None and self.valid_id(local_id) and state["local_id"] == local_id
                    and isinstance(reason, int) and not isinstance(reason, bool) and 1 <= reason <= 4):
                self.active.pop(source_id, None)
                self.object.NotificationClosed(self.dbus.UInt32(source_id), self.dbus.UInt32(reason))
        elif kind == "delivery-error":
            if self.active.pop(source_id, None) is not None:
                self.object.NotificationClosed(self.dbus.UInt32(source_id), self.dbus.UInt32(4))

    def allocate_id(self):
        for _ in range(MAX_ACTIVE + 1):
            candidate = self.next_id
            self.next_id = 1 if candidate == 0xFFFFFFFF else candidate + 1
            if candidate not in self.active:
                return candidate
        raise self.dbus.DBusException("notification ID space exhausted")

    def notify(self, app_name, replaces_id, summary, body, actions, hints, expire_timeout):
        if self.closed:
            raise self.dbus.DBusException("notification transport is unavailable")
        if self.apps is not None and not any(term in str(app_name).lower() for term in self.apps):
            raise self.dbus.DBusException("notification app is outside the forwarding allowlist")
        raw_actions = [str(item) for item in actions]
        if len(raw_actions) > 32 or len(raw_actions) % 2 or any(len(item) > 256 for item in raw_actions):
            raise self.dbus.DBusException("invalid action list")
        replaces = int(replaces_id)
        source_id = (replaces if replaces in self.active and not self.active[replaces]["close_requested"]
                     else self.allocate_id())
        if source_id not in self.active and len(self.active) >= MAX_ACTIVE:
            raise self.dbus.DBusException("notification active-ID limit reached")
        old_state = self.active.get(source_id, {})
        revision = old_state.get("revision", 0) + 1
        if not self.valid_id(revision):
            raise self.dbus.DBusException("notification revision limit reached")
        self.active[source_id] = {
            "actions": raw_actions[::2], "local_id": old_state.get("local_id", 0),
            "close_requested": False, "revision": revision,
            "mapped_revision": old_state.get("mapped_revision", 0),
        }
        urgency = 1
        try:
            urgency = int(hints.get("urgency", 1))
        except (TypeError, ValueError, AttributeError):
            pass
        if urgency not in (0, 1, 2):
            urgency = 1
        record = self.envelope(
            "notify", source_id=source_id, revision=revision, replaces_id=replaces if replaces == source_id else 0,
            app_name=str(app_name)[:128], summary=str(summary)[:512], body=str(body)[:4096],
            actions=raw_actions, urgency=urgency, expires=int(expire_timeout),
            desktop_entry=str(hints.get("desktop-entry", ""))[:128],
        )
        try:
            self.queue(record)
        except Exception:
            self.active.pop(source_id, None)
            raise
        return self.dbus.UInt32(source_id)

    def close(self, notification_id):
        source_id = int(notification_id)
        state = self.active.get(source_id)
        if state is None:
            raise self.dbus.DBusException("unknown notification ID")
        if state["close_requested"]:
            return
        state["close_requested"] = True
        self.active[source_id] = state
        local_id = state["local_id"] if state["mapped_revision"] == state["revision"] else 0
        self.queue(self.envelope("close", source_id=source_id, revision=state["revision"], local_id=local_id))

    def disconnect(self, reason):
        if self.closed:
            return
        self.closed = True
        self.active.clear()
        self.output.clear()
        self.output_bytes = 0
        print("notify-endpoint: transport unavailable: " + reason, file=sys.stderr)
        try:
            self.channel.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.loop.quit()
        except AttributeError:
            pass

    def run(self):
        self.queue(self.envelope("active"))
        self.loop.run()
        self.object.remove_from_connection()


def inspect_owner(bus, dbus, status_path):
    try:
        owner = str(bus.get_name_owner(NAME))
        service = dbus.Interface(bus.get_object(owner, PATH, introspect=False), NAME)
        information = service.GetServerInformation(timeout=0.35)
        if str(information[0]) == MANAGED_SERVER and str(information[1]) == MANAGED_VENDOR:
            write_status(status_path, "busy-managed")
            return "busy-managed"
        capabilities = set(map(str, service.GetCapabilities(timeout=0.35)))
        if not {"body", "actions"}.issubset(capabilities):
            write_status(status_path, "unavailable")
            return "unavailable"
        write_status(status_path, "busy-desktop")
        return "busy-desktop"
    except Exception:
        write_status(status_path, "unavailable")
        return "unavailable"


def main(argv):
    if len(argv) != 4:
        return 64
    socket_path, apps, status_path = argv[1:]
    try:
        import dbus
        import dbus.service
        from dbus.mainloop.glib import DBusGMainLoop
        from gi.repository import GLib
    except Exception as error:
        write_status(status_path, "unavailable")
        print("notify-endpoint: DBus bindings unavailable: " + str(error), file=sys.stderr)
        return 1
    DBusGMainLoop(set_as_default=True)
    address = os.environ.get("DBUS_SESSION_BUS_ADDRESS", "")
    if not address:
        write_status(status_path, "unavailable")
        return 1
    try:
        bus = dbus.bus.BusConnection(address)
    except Exception as error:
        write_status(status_path, "unavailable")
        print("notify-endpoint: source bus unavailable: " + str(error), file=sys.stderr)
        return 1
    channel = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    channel.settimeout(0.5)
    try:
        channel.connect(socket_path)
    except OSError as error:
        write_status(status_path, "unavailable")
        print("notify-endpoint: relay unavailable: " + str(error), file=sys.stderr)
        return 1

    session_id = os.environ.get("REMOTE_CHROME_ORIGIN_SESSION", "remote-chrome")[:256] or "remote-chrome"
    generation = secrets.token_hex(24)
    try:
        send_frame(channel, {"v": PROTOCOL_VERSION, "type": "hello", "session_id": session_id,
                             "generation": generation, "notification_revisions": True})
        response = read_frame(channel, time.monotonic() + 1.2)
        if (not isinstance(response, dict) or type(response.get("v")) is not int
                or response["v"] != PROTOCOL_VERSION or response.get("type") != "ready" or response.get("session_id") != session_id
                or response.get("generation") != generation):
            message = (response.get("message", response.get("code", "listener rejected handshake"))
                       if isinstance(response, dict) else "invalid handshake")
            write_status(status_path, "unavailable")
            print("notify-endpoint: " + str(message), file=sys.stderr)
            return 1
        capabilities = response.get("capabilities", [])
        if (not isinstance(capabilities, list) or any(not isinstance(item, str) for item in capabilities)
                or not {"body", "actions"}.issubset(capabilities)
                or not isinstance(response.get("destination_owner"), str)
                or not response["destination_owner"].startswith(":")):
            write_status(status_path, "unavailable")
            print("notify-endpoint: destination does not support body/actions or pinned ownership", file=sys.stderr)
            return 1
        if response.get("notification_revisions") is not True:
            write_status(status_path, "unavailable")
            print("notify-endpoint: notification listener lacks revision support; update the forwarding host before resetting",
                  file=sys.stderr)
            return 1
    except Exception as error:
        write_status(status_path, "unavailable")
        print("notify-endpoint: handshake failed: " + str(error), file=sys.stderr)
        return 1

    name_result = bus.request_name(NAME, 4)
    if int(name_result) != 1:
        state = inspect_owner(bus, dbus, status_path)
        try:
            send_frame(channel, {"v": PROTOCOL_VERSION, "type": "abort", "session_id": session_id, "generation": generation,
                                 "reason": state})
        except OSError:
            pass
        return 20 if state == "busy-managed" else 0

    channel.setblocking(False)
    endpoint = NotificationEndpoint(dbus, bus, channel, session_id, generation, capabilities, apps)
    try:
        caps = ",".join(sorted(endpoint.capabilities))
        write_status(status_path, "ready\t" + caps)
        print("notify-endpoint: ready caps=" + caps, file=sys.stderr)
        signal.signal(signal.SIGTERM, lambda *_: endpoint.loop.quit())
        signal.signal(signal.SIGINT, lambda *_: endpoint.loop.quit())
        endpoint.run()
        return 0
    finally:
        endpoint.disconnect("endpoint-exit")
        try:
            bus.release_name(NAME)
        except Exception:
            pass
        try:
            bus.close()
        except Exception:
            pass
        write_status(status_path, "stopped")


if __name__ == "__main__":
    sys.exit(main(sys.argv))
REMOTE_CHROME_NOTIFY_ENDPOINT
      chmod 600 -- "$notify_forwarder_script"
      : >"$notify_ready_file"
      chmod 600 -- "$notify_ready_file"
      notify_forwarder_script_inode="$(stat -c '%d:%i' "$notify_forwarder_script" 2>/dev/null || true)"
      notify_ready_file_inode="$(stat -c '%d:%i' "$notify_ready_file" 2>/dev/null || true)"
      python3 "$notify_forwarder_script" "$notify_socket" "$notify_apps" "$notify_ready_file" \
        2>>"$notify_helper_log" &
      notify_process_description="notification endpoint"
      remote_notify_record_process
      for ((attempt = 0; attempt < 20; attempt++)); do
        [ -s "$notify_ready_file" ] && break
        kill -0 "$notify_pid" >/dev/null 2>&1 || break
        sleep 0.1
      done
      helper_state=""
      IFS=$'\t' read -r helper_state notify_capabilities <"$notify_ready_file" 2>/dev/null || true
      case "$helper_state" in
        ready)
          notify_backend="helper"
          ;;
        busy-managed)
          remote_secure_fail "another managed headless notification session won the service-name race; refusing a competing notifications-enabled Chrome session."
          ;;
        busy-desktop)
          notify_backend="desktop"
          notify_capabilities="informational"
          remote_secure_stop_notify_process || remote_secure_fail "could not stop losing notification endpoint helper."
          ;;
        *)
          notify_error="$helper_state"
          [ -n "$notify_error" ] || notify_error="endpoint startup timed out"
          echo "remote-chrome: notifications unavailable ($notify_error); Chrome will start securely without notification forwarding." >&2
          notify_backend="unavailable"
          remote_secure_stop_notify_process || true
          ;;
      esac
    else
      notify_error="notification relay socket or Python runtime is unavailable"
      echo "remote-chrome: notifications unavailable ($notify_error); Chrome will start securely without notification forwarding." >&2
      notify_backend="unavailable"
    fi
  else
    notify_error="could not probe the source notification owner without activation"
    echo "remote-chrome: notifications unavailable ($notify_error); Chrome will start securely without notification forwarding." >&2
    notify_backend="unavailable"
  fi
fi

if [ -n "$notify_socket" ] && [ "$notify_backend" = "desktop" ]; then
  if [ -S "$notify_socket" ]; then
    notify_socket_inode="$(stat -c '%d:%i' "$notify_socket" 2>/dev/null || true)"
  fi
  notify_forwarder_script="$runtime_dir/remote-chrome-notify-forwarder-${BASHPID:-$$}.py"
  cat >"$notify_forwarder_script" <<'REMOTE_CHROME_NOTIFY_FORWARDER'
import json
import signal
import socket
import subprocess
import sys

NOTIFY_INTERFACE = "org.freedesktop.Notifications"
BUSCTL = ["busctl", "--user", "monitor", "--json=short", NOTIFY_INTERFACE]
MAX_RECORD = 1 << 20


def parse_apps(argument):
    if argument.strip() == "*":
        return None
    apps = [term.strip().lower() for term in argument.split(",") if term.strip()]
    return apps or None


def matches(app_name, apps):
    if apps is None:
        return True
    lowered = app_name.lower()
    return any(term in lowered for term in apps)


def hint(data, name):
    hints = data[6] if len(data) > 6 else None
    if not isinstance(hints, dict):
        return None
    entry = hints.get(name)
    if isinstance(entry, dict):
        return entry.get("data")
    return None


def extract(message):
    if not isinstance(message, dict):
        return None
    if message.get("type") != "method_call":
        return None
    if message.get("member") != "Notify":
        return None
    if message.get("interface") != NOTIFY_INTERFACE:
        return None
    payload = message.get("payload")
    if not isinstance(payload, dict):
        return None
    data = payload.get("data")
    if not isinstance(data, list) or len(data) < 8:
        return None
    app_name = data[0] if isinstance(data[0], str) else ""
    return {
        "app_name": app_name,
        "replaces_id": data[1] if isinstance(data[1], int) else 0,
        "summary": data[3] if isinstance(data[3], str) else "",
        "body": data[4] if isinstance(data[4], str) else "",
        "urgency": hint(data, "urgency"),
        "desktop_entry": hint(data, "desktop-entry"),
    }


def send(socket_path, blob):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(5)
        client.connect(socket_path)
        client.sendall(blob)


def main(argv):
    if len(argv) < 2 or len(argv) > 3:
        print("usage: notify-forwarder.py SOCKET [APPS]", file=sys.stderr)
        return 64
    socket_path = argv[1]
    apps = parse_apps(argv[2] if len(argv) > 2 else "chrome")

    def stop(_signum, _frame):
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    monitor = subprocess.Popen(
        BUSCTL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        errors="replace",
    )
    unavailable = False
    try:
        for line in monitor.stdout:
            try:
                message = json.loads(line)
            except ValueError:
                continue
            record = extract(message)
            if record is None or not matches(record["app_name"], apps):
                continue
            blob = (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8")
            if len(blob) > MAX_RECORD:
                continue
            try:
                send(socket_path, blob)
                unavailable = False
            except OSError as error:
                if not unavailable:
                    print(
                        f"notify-forwarder: relay unavailable: {error}",
                        file=sys.stderr,
                    )
                    unavailable = True
    finally:
        monitor.terminate()
        try:
            monitor.wait(timeout=5)
        except subprocess.TimeoutExpired:
            monitor.kill()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except KeyboardInterrupt:
        sys.exit(130)
REMOTE_CHROME_NOTIFY_FORWARDER
  chmod 600 -- "$notify_forwarder_script"
  notify_forwarder_script_inode="$(stat -c '%d:%i' "$notify_forwarder_script" 2>/dev/null || true)"
  python3 "$notify_forwarder_script" "$notify_socket" "$notify_apps" 2>>"$notify_helper_log" &
  notify_process_description="notify forwarder"
  remote_notify_record_process
fi

if [ -n "$incoming_state" ] && [ -f "$incoming_state" ]; then
  incoming_notify_pending="$incoming_state.notify-$$"
  if {
    awk -F '\t' '$1 !~ /^notify_/' "$incoming_state"
    printf 'notify_backend\t%s\n' "$notify_backend"
    printf 'notify_pid\t%s\n' "$notify_pid"
    printf 'notify_starttime\t%s\n' "$notify_starttime"
    printf 'notify_capabilities\t%s\n' "$notify_capabilities"
    printf 'notify_error\t%s\n' "$notify_error"
    printf 'notify_helper_log\t%s\n' "$notify_helper_log"
    printf 'notify_status_file\t%s\n' "$notify_ready_file"
  } >"$incoming_notify_pending" && mv -fT -- "$incoming_notify_pending" "$incoming_state"; then
    chmod 600 -- "$incoming_state"
  else
    rm -f -- "$incoming_notify_pending" 2>/dev/null || true
    echo "remote-chrome: notification status could not be recorded in incoming session state." >&2
  fi
fi

export DBUS_SESSION_BUS_ADDRESS="unix:path=$proxy_socket"
if ! secret-tool lookup application "$secret_application" \
  xdg:schema "$secret_schema" >/dev/null 2>&1; then
  remote_secure_fail "Safe Storage lookup failed or was canceled; Chrome will not start and basic password storage is never allowed."
fi

# Run Chrome as a child so EXIT/signal cleanup always owns the proxy and any
# KWallet Secret Service started above. The caller's exit status is preserved.
"$chrome_command" --password-store=gnome-libsecret "$@" &
chrome_pid=$!
chrome_starttime="$(remote_secure_pid_starttime "$chrome_pid" 2>/dev/null || true)"
if [ -z "$chrome_starttime" ] && kill -0 "$chrome_pid" >/dev/null 2>&1; then
  remote_secure_fail "could not record the Chrome child PID $chrome_pid starttime; refusing unverified cleanup."
fi
if wait "$chrome_pid"; then
  chrome_status=$?
else
  chrome_status=$?
fi
chrome_pid=""
remote_secure_cleanup || true
if [ "$chrome_status" -eq 0 ] && [ "$cleanup_status" -ne 0 ]; then
  chrome_status="$cleanup_status"
fi
exit "$chrome_status"
