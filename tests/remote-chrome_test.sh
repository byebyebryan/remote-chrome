#!/usr/bin/env bash
# shellcheck disable=SC1091,SC2030,SC2031,SC2032,SC2034,SC2154,SC2329
set -euo pipefail

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=../bin/remote-chrome
source "$repo_root/bin/remote-chrome"

fail() {
  echo "FAIL: $*" >&2
  return 1
}

assert_contains() {
  local haystack="$1"
  local needle="$2"
  [[ "$haystack" == *"$needle"* ]] ||
    fail "expected output to contain '$needle'; got: $haystack"
}

assert_file_missing() {
  [ ! -e "$1" ] || fail "expected file to be absent: $1"
}

test_start_rolls_back_partial_setup() (
  local test_dir
  test_dir="$(mktemp -d)"
  trap 'rm -rf "$test_dir"' EXIT

  yk_remote="test-host"
  yk_control_socket="$test_dir/control.sock"
  yk_usbip_port=3240
  yk_set_runtime_paths
  local events=""

  yk_ensure_local_ready() {
    yk_started_usbipd=1
    yk_usbipd_pid=4242
  }
  yk_ensure_remote_ready() { return 0; }
  yk_first_busid() { printf '%s\n' "5-1.2.2"; }
  yk_open_tunnel() {
    events+="open-tunnel "
    return 0
  }
  yk_ssh() {
    events+="remote-check "
    return 1
  }
  yk_remote_detach_busid() {
    events+="detach:$1 "
    return 0
  }
  yk_close_tunnel() { events+="close-tunnel "; }
  yk_stop_owned_usbipd() { events+="stop-daemon "; }
  sudo() {
    events+="sudo:$* "
    return 0
  }

  if yk_start; then
    fail "start unexpectedly succeeded"
  fi

  assert_contains "$events" "sudo:usbip bind -b 5-1.2.2"
  # Visibility failed before remote attach, so rollback must not detach an
  # unrelated import for this bus ID.
  [[ "$events" != *"detach:5-1.2.2"* ]] || fail "detached a bus ID that was never attached"
  assert_contains "$events" "close-tunnel"
  assert_contains "$events" "sudo:usbip unbind -b 5-1.2.2"
  assert_contains "$events" "stop-daemon"
  assert_file_missing "$yk_state_file"
)

test_second_start_cannot_clean_existing_attempt() (
  local test_dir
  test_dir="$(mktemp -d)"
  trap 'rm -rf "$test_dir"' EXIT

  yk_remote="test-host"
  yk_control_socket="$test_dir/control.sock"
  yk_usbip_port=3240
  yk_set_runtime_paths
  yk_phase="ready"
  yk_readiness="verified-fido"
  yk_active_busid="5-1.2.2"
  yk_bind_state="bound"
  yk_attach_state="attached"
  yk_started_usbipd=1
  yk_usbipd_pid=4242
  yk_write_state
  local events=""
  yk_remote_detach_busid() { events+="detach "; }
  yk_close_tunnel() { events+="close "; }
  yk_stop_owned_usbipd() { events+="daemon "; }
  sudo() { events+="sudo "; return 0; }

  if yk_start; then
    fail "second start unexpectedly succeeded"
  fi
  yk_cleanup_done=0
  yk_cleanup
  [ -f "$yk_state_file" ] || fail "failed second start removed existing state"
  [ -z "$events" ] || fail "failed second start cleaned existing resources: $events"
)

test_stop_removes_orphan_attempt_lock_without_state() (
  local test_dir
  test_dir="$(mktemp -d)"
  trap 'rm -rf "$test_dir"' EXIT

  yk_remote="test-host"
  yk_control_socket="$test_dir/control.sock"
  yk_usbip_port=3240
  yk_set_runtime_paths
  mkdir -- "$yk_attempt_lock"
  local events=""
  yk_remote_detach_busid() { events+="detach "; }
  yk_close_tunnel() { events+="close "; }
  yk_stop_owned_usbipd() { events+="daemon "; }
  sudo() { events+="sudo "; return 0; }

  yk_stop

  assert_file_missing "$yk_attempt_lock"
  [ -z "$events" ] || fail "lock-only cleanup touched USB/IP resources: $events"
)

test_non_owner_cleanup_preserves_existing_attempt_lock() (
  local test_dir
  test_dir="$(mktemp -d)"
  trap 'rm -rf "$test_dir"' EXIT

  yk_remote="test-host"
  yk_control_socket="$test_dir/control.sock"
  yk_usbip_port=3240
  yk_set_runtime_paths
  mkdir -- "$yk_attempt_lock"
  yk_attempt_owned=0
  yk_cleanup_done=0
  local events=""
  yk_stop() { events+="stop "; }

  yk_cleanup

  [ -d "$yk_attempt_lock" ] || fail "non-owner cleanup removed another attempt lock"
  [ -z "$events" ] || fail "non-owner cleanup invoked stop: $events"
)

test_active_attempt_cannot_write_state_after_lock_removal() (
  local test_dir
  test_dir="$(mktemp -d)"
  trap 'rm -rf "$test_dir"' EXIT

  yk_remote="test-host"
  yk_control_socket="$test_dir/control.sock"
  yk_usbip_port=3240
  yk_set_runtime_paths
  yk_attempt_owned=1
  if yk_write_state 2>/dev/null; then
    fail "active attempt wrote state after its lock disappeared"
  fi
  assert_file_missing "$yk_state_file"
)

test_successful_start_records_exact_state() (
  local test_dir
  test_dir="$(mktemp -d)"
  trap 'rm -rf "$test_dir"' EXIT

  yk_remote="test-host"
  yk_control_socket="$test_dir/control.sock"
  yk_usbip_port=3240
  yk_set_runtime_paths

  yk_ensure_local_ready() {
    yk_started_usbipd=1
    yk_usbipd_pid=4242
  }
  yk_ensure_remote_ready() { return 0; }
  yk_first_busid() { printf '%s\n' "5-1.2.2"; }
  yk_open_tunnel() { return 0; }
  yk_ssh() { return 0; }
  yk_remote_probe_readiness() {
    yk_readiness="verified-fido"
    return 0
  }
  yk_remote_detach_busid() { return 0; }
  sudo() { return 0; }

  yk_start

  [ -f "$yk_state_file" ] || fail "start did not write managed state"
  assert_contains "$(cat "$yk_state_file")" $'remote\ttest-host'
  assert_contains "$(cat "$yk_state_file")" $'busid\t5-1.2.2'
  assert_contains "$(cat "$yk_state_file")" $'started_usbipd\t1'
  assert_contains "$(cat "$yk_state_file")" $'usbipd_pid\t4242'
)

test_stop_cleans_only_recorded_busid() (
  local test_dir
  test_dir="$(mktemp -d)"
  trap 'rm -rf "$test_dir"' EXIT

  yk_remote="test-host"
  yk_control_socket="$test_dir/control.sock"
  yk_usbip_port=3240
  yk_set_runtime_paths
  yk_active_busid="5-1.2.2"
  yk_started_usbipd=1
  yk_usbipd_pid=4242
  yk_write_state
  local events=""

  yk_remote_detach_busid() {
    events+="detach:$1 "
    return 0
  }
  yk_close_tunnel() { events+="close-tunnel "; }
  yk_stop_owned_usbipd() { events+="stop-daemon "; }
  sudo() {
    events+="sudo:$* "
    return 0
  }

  yk_stop

  assert_contains "$events" "detach:5-1.2.2"
  assert_contains "$events" "sudo:usbip unbind -b 5-1.2.2"
  assert_contains "$events" "stop-daemon"
  assert_file_missing "$yk_state_file"
)

test_stop_cleans_yubikey_without_yubikey_window() (
  local test_dir
  test_dir="$(mktemp -d)"
  trap 'rm -rf "$test_dir"' EXIT

  XDG_RUNTIME_DIR="$test_dir"
  yk_control_socket=""
  yk_usbip_port=3240
  yk_prepare_for_launch "test-host"
  yk_active_busid="5-1.2.2"
  yk_started_usbipd=1
  yk_usbipd_pid=4242
  yk_write_state
  local events=""

  need() { :; }
  tmux() { events+="kill-session:$* "; return 0; }
  yk_remote_detach_busid() {
    events+="detach:$1 "
    return 0
  }
  yk_close_tunnel() { events+="close-tunnel "; }
  yk_stop_owned_usbipd() { events+="stop-daemon "; }
  sudo() {
    events+="sudo:$* "
    return 0
  }

  chrome_stop "test-host"

  assert_contains "$events" "kill-session"
  assert_contains "$events" "detach:5-1.2.2"
  assert_contains "$events" "sudo:usbip unbind -b 5-1.2.2"
  assert_contains "$events" "stop-daemon"
  assert_file_missing "$yk_state_file"
)

test_stop_without_host_stops_only_managed_tmux_sessions() (
  local events=""

  need() { :; }
  tmux() {
    case "$1" in
      list-sessions)
        printf '%s\n' remote-chrome-host-one remote-chrome remote-chrome-host-two unrelated-session
        ;;
      kill-session)
        events+="kill:$3 "
        ;;
      *) return 1 ;;
    esac
  }
  yk_stop_all() { events+="yubikey-all "; }

  chrome_stop

  assert_contains "$events" "kill:remote-chrome-host-one"
  assert_contains "$events" "kill:remote-chrome-host-two"
  assert_contains "$events" "yubikey-all"
  [[ "$events" != *"kill:remote-chrome "* ]] ||
    fail "global stop killed the exact-prefix non-managed tmux session"
  [[ "$events" != *"kill:unrelated-session"* ]] ||
    fail "global stop killed an unrelated tmux session"
)

test_stop_without_host_cleans_all_recorded_yubikey_states() (
  local test_dir events=""
  test_dir="$(mktemp -d)"
  trap 'rm -rf "$test_dir"' EXIT
  XDG_RUNTIME_DIR="$test_dir"
  yk_control_socket=""
  : >"$test_dir/remote-chrome-yubikey-first.sock.state"
  : >"$test_dir/remote-chrome-yubikey-second.sock.state"

  need() { :; }
  tmux() {
    case "$1" in
      list-sessions) return 0 ;;
      *) return 1 ;;
    esac
  }
  yk_stop_state_file() { events+="state:$1 "; }
  yk_stop_orphan_attempt_locks() { events+="locks "; }

  chrome_stop

  assert_contains "$events" "state:$test_dir/remote-chrome-yubikey-first.sock.state"
  assert_contains "$events" "state:$test_dir/remote-chrome-yubikey-second.sock.state"
  assert_contains "$events" "locks"
)

test_remote_preflight_uses_scoped_sudo_command() (
  yk_remote="test-host"
  local remote_command=""

  yk_ssh() {
    remote_command="$*"
    return 0
  }

  yk_ensure_remote_ready
  assert_contains "$remote_command" "sudo -n modprobe vhci-hcd"
  assert_contains "$remote_command" "command -v timeout"
  [[ "$remote_command" != *"sudo -n true"* ]] ||
    fail "remote preflight still requires unrestricted sudo via true"
)

test_remote_detach_failure_is_reported() (
  local test_dir fake_bin output code=0
  test_dir="$(mktemp -d)"
  trap 'rm -rf "$test_dir"' EXIT
  fake_bin="$test_dir/bin"
  mkdir -p "$fake_bin"

  printf '%s\n' \
    '#!/usr/bin/env bash' \
    'shift' \
    'exec "$@"' >"$fake_bin/timeout"
  # These variables intentionally expand when the generated fake sudo runs.
  # shellcheck disable=SC2016
  printf '%s\n' \
    '#!/usr/bin/env bash' \
    '[ "${1:-}" = "-n" ] && shift' \
    'case "${1:-} ${2:-}" in' \
    '  "usbip port")' \
    '    printf "%s\n" "Port 00: <Port in Use>" "       7-1 -> usbip://127.0.0.1:3240/5-1.2.2"' \
    '    ;;' \
    '  "usbip detach") exit 7 ;;' \
    '  *) exit 2 ;;' \
    'esac' >"$fake_bin/sudo"
  chmod +x "$fake_bin/timeout" "$fake_bin/sudo"

  yk_ssh() {
    local remote_command="$1"
    shift
    [ "$remote_command" = "bash -s" ] || fail "unexpected remote detach command: $remote_command"
    PATH="$fake_bin:$PATH" bash -s "$@"
  }

  output="$(yk_remote_detach_busid "5-1.2.2" 2>&1)" || code=$?
  [ "$code" -ne 0 ] || fail "remote detach failure was swallowed"
  assert_contains "$output" "Could not detach remote USB/IP port 00 for busid 5-1.2.2."
)

test_forwarding_preflight_requires_local_timeout() (
  yk_remote="test-host"
  need() {
    if [ "$1" = "timeout" ]; then
      die "Missing required command: timeout"
    fi
    return 0
  }
  local output code=0
  output="$(yk_preflight_for_forwarding 2>&1)" || code=$?
  [ "$code" -eq 1 ] || fail "forwarding preflight did not reject missing local timeout"
  assert_contains "$output" "Missing required command: timeout"
)

test_usbipd_state_write_failure_stops_before_daemon() (
  local test_dir events=""
  test_dir="$(mktemp -d)"
  trap 'rm -rf "$test_dir"' EXIT

  yk_remote="test-host"
  yk_control_socket="$test_dir/control.sock"
  yk_usbip_port=3240
  yk_set_runtime_paths
  need() { :; }
  yk_local_module_preflight() { :; }
  ss() { return 1; }
  yk_write_state() {
    events+="state "
    return 1
  }
  sudo() {
    case "$1" in
      -v|modprobe|rm) return 0 ;;
      usbipd) events+="daemon "; return 0 ;;
      *) return 0 ;;
    esac
  }

  if yk_ensure_local_ready; then
    fail "usbipd setup unexpectedly succeeded after state persistence failure"
  fi
  assert_contains "$events" "state"
  [[ "$events" != *"daemon"* ]] || fail "usbipd started after provisional state write failed"
)

test_usbipd_state_write_records_pid_before_started_phase() (
  local definition
  definition="$(declare -f yk_ensure_local_ready)"
  [[ "$definition" != *"yk_write_state || true"* ]] ||
    fail "usbipd setup ignored state persistence failures"
  local pid_line phase_line
  pid_line="$(printf '%s\n' "$definition" | awk '/yk_usbipd_pid=.*sudo cat/{print NR; exit}')"
  phase_line="$(printf '%s\n' "$definition" | awk '/yk_phase="usbipd-started"/{print NR; exit}')"
  if [ -z "$pid_line" ] || [ -z "$phase_line" ] || [ "$pid_line" -ge "$phase_line" ]; then
    fail "validated usbipd PID was not read before usbipd-started state"
  fi
)

test_custom_chrome_command_is_in_process_pattern() (
  local pattern
  pattern="$(chrome_process_pattern "/usr/bin/chromium-browser")"

  assert_contains "$pattern" "chromium-browser"
  [[ "/usr/bin/chromium-browser --profile-directory=Default" =~ $pattern ]] ||
    fail "custom Chrome executable did not match its process pattern"
  [[ ! "/usr/bin/chromium-browser-helper --type=utility" =~ $pattern ]] ||
    fail "custom Chrome process pattern matched a longer executable name"
)

test_chrome_command_rejects_shell_syntax() (
  if (chrome_process_pattern "chromium --incognito" >/dev/null 2>&1); then
    fail "Chrome command with shell syntax unexpectedly passed validation"
  fi
)

test_existing_process_check_uses_custom_command() (
  local ssh_arguments=""
  ssh() {
    ssh_arguments="$*"
    return 0
  }

  chrome_existing_processes "test-host" "/usr/bin/chromium-browser"
  assert_contains "$ssh_arguments" "pgrep"
  assert_contains "$ssh_arguments" "chromium-browser"
)

test_existing_process_prompts_and_kills_by_default() (
  local events=""
  ssh() {
    printf '%s\n' "12345 /usr/bin/google-chrome-stable --no-startup-window"
    return 0
  }
  confirm() { events+="prompt "; return 0; }
  chrome_kill_existing() { events+="kill "; }

  chrome_handle_existing "test-host" "google-chrome-stable" 0 0 0
  assert_contains "$events" "prompt"
  assert_contains "$events" "kill"
)

test_existing_process_declined_cancel_launch() (
  local test_dir
  test_dir="$(mktemp -d)"
  trap 'rm -rf "$test_dir"' EXIT
  local events_file="$test_dir/events"
  ssh() {
    printf '%s\n' "12345 /usr/bin/google-chrome-stable --no-startup-window"
    return 0
  }
  confirm() { printf 'prompt ' >>"$events_file"; return 1; }
  chrome_kill_existing() { printf 'kill ' >>"$events_file"; }

  local code=0
  (chrome_handle_existing "test-host" "google-chrome-stable" 0 0 0) 2>/dev/null || code=$?
  [ "$code" = "1" ] || fail "declining the kill prompt should cancel the launch with exit 1"
  assert_contains "$(cat "$events_file")" "prompt"
  [[ "$(cat "$events_file")" != *"kill"* ]] ||
    fail "killed remote Chrome after declining the prompt"
)

test_existing_process_allow_existing_skips_prompt() (
  local events=""
  ssh() {
    printf '%s\n' "12345 /usr/bin/google-chrome-stable --no-startup-window"
    return 0
  }
  confirm() { events+="prompt "; return 0; }
  chrome_kill_existing() { events+="kill "; }

  chrome_handle_existing "test-host" "google-chrome-stable" 1 0 0
  [[ "$events" == "" ]] || fail "allow-existing still prompted or killed"
)

test_existing_process_yes_skips_prompt() (
  local events=""
  ssh() {
    printf '%s\n' "12345 /usr/bin/google-chrome-stable --no-startup-window"
    return 0
  }
  confirm() { events+="prompt "; return 0; }
  chrome_kill_existing() { events+="kill "; }

  chrome_handle_existing "test-host" "google-chrome-stable" 0 1 0
  assert_contains "$events" "kill"
  [[ "$events" != *"prompt"* ]] || fail "--yes still prompted before killing"
)

test_find_busids_matches_usb_id() (
  yk_usb_id="1050:0407"
  # Keep this parser test independent of any YubiKey attached to the test host.
  yk_sysfs_busids() { return 1; }
  usbip() {
    cat <<'EOF'
Bus 001 Device 007: ID 1050:0407 Yubico.com Yubikey 4/5 OTP+U2F+CCID
 - busid 1-1.2.1 (1050:0407)
Bus 001 Device 003: ID 8087:0026 Intel Corp. Hub
 - busid 1-1.1 (8087:0026)
EOF
  }

  local result
  result="$(yk_find_busids)"
  assert_contains "$result" "1-1.2.1"
  [[ "$result" != *"1-1.1"* ]] || fail "non-matching bus id was included"
)

test_internal_yubikey_run_sets_up_forwarding() (
  yk_run() { printf 'ran\n'; }

  _yubikey_run_main "test-host" --usb-id "1050:0407" --port 3300 >/dev/null
  [ "$yk_remote" = "test-host" ] || fail "internal yubikey run set the wrong remote"
  [ "$yk_usb_id" = "1050:0407" ] || fail "internal yubikey run ignored --usb-id"
  [ "$yk_usbip_port" = "3300" ] || fail "internal yubikey run ignored --port"
)

test_sysfs_detection_does_not_require_usbip() (
  yk_usb_id="1050:0407"
  yk_sysfs_busids() { printf '%s\n' "3-1"; }
  [ "$(yk_first_busid)" = "3-1" ] || fail "sysfs YubiKey bus id was not preferred"
)

test_readiness_wait_accepts_delayed_fido() (
  local attempts=0
  REMOTE_CHROME_YUBIKEY_TIMEOUT=3
  REMOTE_CHROME_YUBIKEY_POLL_INTERVAL=0
  yk_remote_probe_readiness() {
    attempts=$((attempts + 1))
    if [ "$attempts" -ge 2 ]; then
      yk_readiness="verified-fido"
      return 0
    fi
    yk_readiness="not-ready"
    return 1
  }

  yk_wait_remote_ready
  [ "$attempts" = "2" ] || fail "readiness polling did not retry once"
  [ "$yk_readiness" = "verified-fido" ] || fail "readiness did not preserve FIDO result"
)

test_readiness_timeout_is_bounded() (
  local attempts=0
  REMOTE_CHROME_YUBIKEY_TIMEOUT=2
  REMOTE_CHROME_YUBIKEY_POLL_INTERVAL=0
  yk_remote_probe_readiness() {
    attempts=$((attempts + 1))
    yk_readiness="not-ready"
    return 1
  }

  if yk_wait_remote_ready 2>/dev/null; then
    fail "readiness timeout unexpectedly succeeded"
  fi
  [ "$attempts" -ge 1 ] || fail "readiness timeout did not probe"
)

test_invalid_timeout_values_fall_back() (
  REMOTE_CHROME_YUBIKEY_TIMEOUT="not-a-number"
  REMOTE_CHROME_YUBIKEY_POLL_INTERVAL="also-invalid"
  yk_remote_probe_readiness() {
    yk_readiness="verified-fido"
    return 0
  }
  yk_wait_remote_ready
  [ "$yk_readiness" = "verified-fido" ] || fail "invalid readiness timeout did not fall back safely"
)

test_readiness_probe_matches_exact_device_and_fido_metadata() (
  local probe_command_file
  probe_command_file="$(mktemp)"
  trap 'rm -f "$probe_command_file"' EXIT
  yk_usb_id="1050:0407"
  yk_ssh() {
    printf '%s' "$*" >"$probe_command_file"
    printf '%s\n' "not-ready"
    return 1
  }
  yk_remote_probe_readiness 2>/dev/null || true
  local probe_command
  probe_command="$(cat "$probe_command_file")"
  assert_contains "$probe_command" "wanted_vendor='1050'"
  assert_contains "$probe_command" "wanted_product='0407'"
  assert_contains "$probe_command" "ID_VENDOR_ID"
  assert_contains "$probe_command" "ID_MODEL_ID"
  assert_contains "$probe_command" "ID_INPUT_FIDO"
  assert_contains "$probe_command" "ID_FIDO_TOKEN"
  [[ "$probe_command" != *"grep -Eiq 'hidraw|yubikey|yubico'"* ]] ||
    fail "readiness probe still accepts an unrelated broad hidraw match"
  [[ "$probe_command" != *"ID_MODEL(_FROM_DATABASE)?=.*(fido|yubi|security)"* ]] ||
    fail "OTP-only YubiKey model metadata was accepted as FIDO metadata"
)

test_readiness_probe_rejects_unrelated_and_otp_hidraw_devices() (
  local test_dir fake_bin fake_bin_no_fido hidraw_root script_file output
  test_dir="$(mktemp -d)"
  trap 'rm -rf "$test_dir"' EXIT
  fake_bin="$test_dir/bin"
  fake_bin_no_fido="$test_dir/bin-no-fido"
  hidraw_root="$test_dir/hidraw"
  script_file="$test_dir/probe.sh"
  mkdir -p "$fake_bin" "$fake_bin_no_fido" "$hidraw_root"
  : >"$hidraw_root/hidraw-unrelated"
  : >"$hidraw_root/hidraw-desired"
  printf '%s\n' '#!/usr/bin/env bash' 'printf "%s\\n" "/dev/hidraw-unrelated: unrelated token" "/dev/hidraw-desired: desired token"' >"$fake_bin/fido2-token"
  chmod +x "$fake_bin/fido2-token"
  printf '%s\n' '#!/usr/bin/env bash' 'case "$*" in' '*unrelated*) printf "%s\\n" "ID_VENDOR_ID=1234" "ID_MODEL_ID=5678" "ID_INPUT_FIDO=1" ;;' '*) printf "%s\\n" "ID_VENDOR_ID=1050" "ID_MODEL_ID=0407" "ID_FIDO_TOKEN=1" ;;' 'esac' >"$fake_bin/udevadm"
  chmod +x "$fake_bin/udevadm"
  cp "$fake_bin/udevadm" "$fake_bin_no_fido/udevadm"

  yk_usb_id="1050:0407"
  yk_ssh() { printf '%s' "$1" >"$script_file"; return 1; }
  yk_remote_probe_readiness >/dev/null 2>&1 || true
  output="$(PATH="$fake_bin:$PATH" REMOTE_CHROME_HIDRAW_ROOT="$hidraw_root" bash "$script_file")"
  [ "$output" = "verified-fido" ] || fail "desired exact FIDO device was not selected: $output"

  printf '%s\n' '#!/usr/bin/env bash' 'case "$*" in' '*) printf "%s\\n" "ID_VENDOR_ID=1050" "ID_MODEL_ID=0407" "ID_INPUT_KEY=1" ;;' 'esac' >"$fake_bin_no_fido/udevadm"
  chmod +x "$fake_bin_no_fido/udevadm"
  printf '%s\n' '#!/usr/bin/env bash' 'exit 127' >"$fake_bin_no_fido/fido2-token"
  chmod +x "$fake_bin_no_fido/fido2-token"
  output="$(PATH="$fake_bin_no_fido:/usr/bin:/bin" REMOTE_CHROME_HIDRAW_ROOT="$hidraw_root" bash "$script_file")" || true
  [ "$output" = "not-ready" ] || fail "OTP-only hidraw metadata satisfied readiness: $output"
)

test_parent_wait_preserves_bootstrap_and_readiness_windows() (
  local test_dir calls=0
  test_dir="$(mktemp -d)"
  trap 'rm -rf "$test_dir"' EXIT
  yk_state_file="$test_dir/state"
  yk_log_file="$test_dir/log"
  yk_remote="test-host"
  yk_phase="starting"
  yk_readiness="none"
  yk_write_state
  REMOTE_CHROME_YUBIKEY_TIMEOUT=2
  REMOTE_CHROME_YUBIKEY_BOOTSTRAP_TIMEOUT=4
  tmux() {
    case "$1" in
      has-session|list-panes) return 0 ;;
      *) return 0 ;;
    esac
  }
  sleep() {
    calls=$((calls + 1))
    SECONDS=$((SECONDS + 1))
    if [ "$calls" -ge 2 ]; then
      yk_phase="ready"
      yk_readiness="verified-fido"
      yk_write_state
    fi
  }

  chrome_wait_for_yubikey_window "test-host" "test-session"
  [ "$calls" -ge 2 ] || fail "parent readiness wait ended before bootstrap completed"
)

test_stop_cleans_provisional_state_without_busid() (
  local test_dir
  test_dir="$(mktemp -d)"
  trap 'rm -rf "$test_dir"' EXIT

  yk_remote="test-host"
  yk_control_socket="$test_dir/control.sock"
  yk_usbip_port=3240
  yk_set_runtime_paths
  yk_phase="bound"
  yk_active_busid=""
  yk_started_usbipd=1
  yk_usbipd_pid=4242
  yk_write_state
  local events=""
  yk_close_tunnel() { events+="close-tunnel "; }
  yk_stop_owned_usbipd() { events+="stop-daemon "; }
  sudo() { events+="sudo:$* "; return 0; }

  yk_stop
  [[ "$events" == *close-tunnel* ]] || fail "provisional cleanup did not close tunnel"
  [[ "$events" == *stop-daemon* ]] || fail "provisional cleanup did not stop owned daemon"
  [[ "$events" != *"usbip unbind"* ]] || fail "provisional cleanup attempted an unscoped unbind"
  assert_file_missing "$yk_state_file"
)

test_stop_loads_earliest_usbipd_provisional_phase() (
  local test_dir
  test_dir="$(mktemp -d)"
  trap 'rm -rf "$test_dir"' EXIT

  yk_remote="test-host"
  yk_control_socket="$test_dir/control.sock"
  yk_usbip_port=3240
  yk_set_runtime_paths
  yk_phase="usbipd-starting"
  yk_started_usbipd=1
  yk_usbipd_pid=""
  yk_write_state
  printf '%s\n' 4242 >"$yk_usbipd_pid_file"
  local events=""
  yk_owned_usbipd_running() { [ "$1" = "4242" ]; }
  yk_other_exports_exist() { return 1; }
  yk_close_tunnel() { :; }
  sudo() {
    if [ "$1" = "cat" ]; then
      command cat "$2"
    else
      events+="sudo:$* "
    fi
    return 0
  }

  yk_stop
  assert_contains "$events" "kill 4242"
  [[ "$events" != *"unbind"* ]] || fail "earliest provisional phase attempted unbind"
  assert_file_missing "$yk_state_file"
)

test_status_reports_yubikey_phase_without_cleanup() (
  local test_dir
  test_dir="$(mktemp -d)"
  trap 'rm -rf "$test_dir"' EXIT
  XDG_RUNTIME_DIR="$test_dir"
  yk_control_socket=""
  yk_usbip_port=3240
  yk_prepare_for_launch "test-host"
  yk_phase="waiting-readiness"
  yk_readiness="waiting"
  yk_active_busid="5-1.2.2"
  yk_bind_state="bound"
  yk_attach_state="attempting"
  yk_write_state

  need() { :; }
  tmux() {
    case "$1" in
      has-session) return 1 ;;
      *) return 0 ;;
    esac
  }
  local output
  output="$(chrome_status test-host)"
  assert_contains "$output" "yubikey: phase=waiting-readiness readiness=waiting"
  assert_contains "$output" "busid=5-1.2.2"
  [ -f "$yk_state_file" ] || fail "status removed managed state"
)

test_status_reports_provisional_setup_lock() (
  local test_dir output
  test_dir="$(mktemp -d)"
  trap 'rm -rf "$test_dir"' EXIT
  XDG_RUNTIME_DIR="$test_dir"
  yk_control_socket=""
  yk_usbip_port=3240
  yk_prepare_for_launch "test-host"
  mkdir -- "$yk_attempt_lock"

  need() { :; }
  tmux() {
    case "$1" in
      has-session) return 1 ;;
      *) return 0 ;;
    esac
  }

  output="$(chrome_status test-host)"
  assert_contains "$output" "yubikey: phase=starting readiness=unknown"
  assert_contains "$output" "provisional-lock=$yk_attempt_lock"
  [ -d "$yk_attempt_lock" ] || fail "status removed provisional setup lock"
)

test_status_without_host_reports_managed_overview() (
  local test_dir output
  test_dir="$(mktemp -d)"
  trap 'rm -rf "$test_dir"' EXIT
  XDG_RUNTIME_DIR="$test_dir"
  yk_control_socket=""
  yk_usbip_port=3240
  yk_prepare_for_launch "host-one"
  yk_phase="ready"
  yk_readiness="verified-fido"
  yk_active_busid="5-1.2.2"
  yk_bind_state="bound"
  yk_attach_state="attached"
  yk_write_state
  mkdir -- "$test_dir/remote-chrome-yubikey-orphan.sock.state.lock"

  need() { :; }
  tmux() {
    case "$1" in
      list-sessions)
        printf '%s\n' remote-chrome-host-one unrelated-session
        ;;
      list-windows)
        printf '%s\n' "    window: chrome (0)"
        ;;
      *) return 1 ;;
    esac
  }

  output="$(chrome_status)"
  assert_contains "$output" "Managed tmux sessions:"
  assert_contains "$output" "remote-chrome-host-one"
  [[ "$output" != *unrelated-session* ]] || fail "status included an unrelated tmux session"
  assert_contains "$output" "host-one: phase=ready readiness=verified-fido busid=5-1.2.2"
  assert_contains "$output" "provisional setup lock: $test_dir/remote-chrome-yubikey-orphan.sock.state.lock"
  [ -f "$yk_state_file" ] || fail "overview status removed managed state"
)

test_attach_uses_tmux_context_appropriate_action() (
  local events=""

  need() { :; }
  tmux() {
    case "$1" in
      has-session) return 0 ;;
      attach-session) events+="attach:$3 ";;
      switch-client) events+="switch:$3 ";;
      *) return 1 ;;
    esac
  }

  unset TMUX
  chrome_attach test-host
  assert_contains "$events" "attach:remote-chrome-test-host"

  events=""
  TMUX="/tmp/tmux-test/default,1,0"
  chrome_attach test-host
  assert_contains "$events" "switch:remote-chrome-test-host"
)

test_subcommands_accept_help() (
  local output

  output="$(main launch --help)"
  assert_contains "$output" "Usage:"
  output="$(main stop --help)"
  assert_contains "$output" "Usage:"
  output="$(main status --help)"
  assert_contains "$output" "Usage:"
  output="$(main attach --help)"
  assert_contains "$output" "Usage:"
)

test_status_reports_legacy_state_truthfully() (
  local test_dir output
  test_dir="$(mktemp -d)"
  trap 'rm -rf "$test_dir"' EXIT
  XDG_RUNTIME_DIR="$test_dir"
  yk_control_socket=""
  yk_usbip_port=3240
  yk_prepare_for_launch "test-host"
  printf '%s\n' \
    $'remote\ttest-host' \
    $'busid\t5-1.2.2' \
    $'started_usbipd\t0' \
    $'usbipd_pid\t' >"$yk_state_file"

  need() { :; }
  tmux() {
    case "$1" in
      has-session) return 1 ;;
      *) return 0 ;;
    esac
  }

  output="$(chrome_status test-host)"
  assert_contains "$output" "yubikey: phase=legacy-active readiness=unknown busid=5-1.2.2"

  printf '%s\n' \
    $'remote\ttest-host' \
    $'started_usbipd\t1' \
    $'usbipd_pid\t4242' >"$yk_state_file"
  output="$(chrome_status test-host)"
  assert_contains "$output" "yubikey: phase=legacy-provisional readiness=unknown"
)

test_stop_cleans_legacy_state_without_lock_or_token() (
  local test_dir events=""
  test_dir="$(mktemp -d)"
  trap 'rm -rf "$test_dir"' EXIT

  yk_remote="test-host"
  yk_control_socket="$test_dir/control.sock"
  yk_usbip_port=3240
  yk_set_runtime_paths
  printf '%s\n' \
    $'remote\ttest-host' \
    $'busid\t5-1.2.2' \
    $'started_usbipd\t0' \
    $'usbipd_pid\t' >"$yk_state_file"
  yk_remote_detach_busid() { events+="detach:$1 "; }
  yk_close_tunnel() { events+="close "; }
  yk_stop_owned_usbipd() { events+="daemon "; }
  sudo() { events+="sudo:$* "; return 0; }

  yk_stop

  assert_contains "$events" "detach:5-1.2.2"
  assert_contains "$events" "sudo:usbip unbind -b 5-1.2.2"
  assert_file_missing "$yk_state_file"
)

test_second_start_preserves_live_legacy_state() (
  local test_dir events="" state_before
  test_dir="$(mktemp -d)"
  trap 'rm -rf "$test_dir"' EXIT

  yk_remote="test-host"
  yk_control_socket="$test_dir/control.sock"
  yk_usbip_port=3240
  yk_set_runtime_paths
  printf '%s\n' \
    $'remote\ttest-host' \
    $'busid\t5-1.2.2' \
    $'started_usbipd\t0' \
    $'usbipd_pid\t' >"$yk_state_file"
  state_before="$(cat "$yk_state_file")"
  yk_remote_detach_busid() { events+="detach "; }
  yk_close_tunnel() { events+="close "; }
  yk_stop_owned_usbipd() { events+="daemon "; }
  sudo() { events+="sudo "; return 0; }

  if yk_start; then
    fail "duplicate start unexpectedly accepted a legacy managed state"
  fi
  yk_cleanup_done=0
  yk_cleanup

  [ "$(cat "$yk_state_file")" = "$state_before" ] ||
    fail "duplicate start changed live legacy state"
  [ -z "$events" ] || fail "duplicate start cleaned legacy resources: $events"
)

test_local_missing_module_tree_has_diagnostics() (
  local test_dir output code=0
  test_dir="$(mktemp -d)"
  trap 'rm -rf "$test_dir"' EXIT
  REMOTE_CHROME_MODULES_ROOT="$test_dir/missing-modules"
  output="$(yk_local_module_preflight 2>&1)" || code=$?
  [ "$code" -ne 0 ] || fail "missing local module tree unexpectedly passed"
  assert_contains "$output" "running kernel"
  assert_contains "$output" "available $REMOTE_CHROME_MODULES_ROOT directories"
  assert_contains "$output" "reboot"
)

test_local_missing_module_has_diagnostics() (
  local test_dir output code=0 kernel
  test_dir="$(mktemp -d)"
  trap 'rm -rf "$test_dir"' EXIT
  kernel="$(uname -r)"
  mkdir -p "$test_dir/$kernel"
  REMOTE_CHROME_MODULES_ROOT="$test_dir"
  output="$(yk_local_module_preflight 2>&1)" || code=$?
  [ "$code" -ne 0 ] || fail "missing usbip-host module unexpectedly passed"
  assert_contains "$output" "required module: usbip-host"
  assert_contains "$output" "running kernel"
)

test_remote_preflight_failure_is_propagated() (
  yk_remote="test-host"
  yk_ssh() { return 1; }
  local output code=0
  output="$(yk_ensure_remote_ready 2>&1)" || code=$?
  [ "$code" -ne 0 ] || fail "remote preflight failure unexpectedly passed"
  assert_contains "$output" "Remote YubiKey preflight failed"
)

test_other_exports_ignores_module_symlink() (
  local test_dir
  test_dir="$(mktemp -d)"
  trap 'rm -rf "$test_dir"' EXIT
  yk_usbip_host_sysfs="$test_dir/usbip-host"
  mkdir -p "$yk_usbip_host_sysfs"
  ln -s /dev/null "$yk_usbip_host_sysfs/module"
  yk_other_exports_exist && fail "permanent module symlink counted as an export"
  ln -s /dev/null "$yk_usbip_host_sysfs/1-2.3"
  yk_other_exports_exist || fail "real USB bus ID symlink was not detected"
)

test_launch_mode_auto_and_opt_out() (
  local test_dir
  test_dir="$(mktemp -d)"
  trap 'rm -rf "$test_dir"' EXIT
  local events=""
  WAYLAND_DISPLAY=wayland-0
  need() { :; }
  chrome_preflight() { :; }
  chrome_handle_existing() { events+="existing "; }
  yk_prepare_for_launch() {
    yk_remote="$1"
    yk_control_socket="$test_dir/control.sock"
    yk_set_runtime_paths
  }
  yk_local_candidate_exists() { return 0; }
  yk_preflight_for_forwarding() { events+="preflight "; }
  chrome_launch_detached_with_yubikey() { events+="yubikey-launch "; }
  tmux() {
    case "$1" in
      has-session) return 1 ;;
      new-session) events+="new-session "; return 0 ;;
      *) return 0 ;;
    esac
  }
  local output
  chrome_launch test-host >"$test_dir/launch-output"
  output="$(command cat "$test_dir/launch-output")"
  [[ "$events" == *preflight* ]] || fail "auto mode did not expect forwarding"
  [[ "$events" == *"preflight existing"* ]] || fail "existing Chrome handling ran before YubiKey preflight"
  assert_contains "$output" "Attach: $PROGRAM attach test-host --session remote-chrome-test-host"
  assert_contains "$output" "Status: $PROGRAM status test-host --session remote-chrome-test-host"
  assert_contains "$output" "Stop: $PROGRAM stop test-host --session remote-chrome-test-host"

  events=""
  chrome_launch test-host --no-yubikey >/dev/null
  [[ "$events" != *preflight* ]] || fail "--no-yubikey still ran forwarding preflight"

  events=""
  yk_local_candidate_exists() { return 1; }
  chrome_launch test-host >/dev/null
  [[ "$events" != *preflight* ]] || fail "no-key auto mode still ran forwarding preflight"
  [[ "$events" == *new-session* ]] || fail "no-key auto mode did not launch Chrome-only tmux"
)

test_detached_duplicate_checks_tmux_before_existing_chrome() (
  local test_dir events_file code=0
  test_dir="$(mktemp -d)"
  trap 'rm -rf "$test_dir"' EXIT
  events_file="$test_dir/events"
  WAYLAND_DISPLAY=wayland-0
  need() { :; }
  chrome_preflight() { :; }
  chrome_handle_existing() { printf 'existing ' >>"$events_file"; }
  yk_prepare_for_launch() {
    yk_remote="$1"
    yk_control_socket="$test_dir/control.sock"
    yk_set_runtime_paths
  }
  yk_local_candidate_exists() { return 1; }
  tmux() {
    case "$1" in
      has-session) printf 'tmux ' >>"$events_file"; return 0 ;;
      *) return 0 ;;
    esac
  }

  (chrome_launch test-host) >/dev/null 2>&1 || code=$?
  [ "$code" -eq 1 ] || fail "duplicate detached launch did not abort"
  local events
  events="$(cat "$events_file" 2>/dev/null || true)"
  assert_contains "$events" "tmux"
  [[ "$events" != *existing* ]] || fail "duplicate detached launch touched existing Chrome first"
)

test_foreground_duplicate_checks_yubikey_state_before_existing_chrome() (
  local test_dir events_file code=0
  test_dir="$(mktemp -d)"
  trap 'rm -rf "$test_dir"' EXIT
  events_file="$test_dir/events"
  WAYLAND_DISPLAY=wayland-0
  need() { :; }
  chrome_preflight() { :; }
  chrome_handle_existing() { printf 'existing ' >>"$events_file"; }
  yk_prepare_for_launch() {
    yk_remote="$1"
    yk_control_socket="$test_dir/control.sock"
    yk_set_runtime_paths
  }
  yk_local_candidate_exists() { return 0; }
  yk_preflight_for_forwarding() { printf 'preflight ' >>"$events_file"; }
  mkdir -p "$test_dir"

  # A provisional lock represents another forwarding attempt even before it
  # has written managed state.
  yk_prepare_for_launch test-host
  mkdir -- "$yk_attempt_lock"
  (chrome_launch test-host --foreground) >/dev/null 2>&1 || code=$?
  [ "$code" -eq 1 ] || fail "duplicate foreground launch did not abort"
  local events
  events="$(cat "$events_file" 2>/dev/null || true)"
  [[ "$events" != *existing* ]] || fail "duplicate foreground launch touched existing Chrome first"
  [[ "$events" != *preflight* ]] || fail "duplicate foreground launch ran forwarding setup first"
)

test_version_flag_reports_version() (
  local output
  output="$(main --version)"
  assert_contains "$output" "$VERSION"
  [[ "$output" == *"$PROGRAM"* ]] || fail "version output omitted the program name"
)

tests=(
  test_start_rolls_back_partial_setup
  test_second_start_cannot_clean_existing_attempt
  test_stop_removes_orphan_attempt_lock_without_state
  test_non_owner_cleanup_preserves_existing_attempt_lock
  test_active_attempt_cannot_write_state_after_lock_removal
  test_successful_start_records_exact_state
  test_stop_cleans_only_recorded_busid
  test_stop_cleans_yubikey_without_yubikey_window
  test_stop_without_host_stops_only_managed_tmux_sessions
  test_stop_without_host_cleans_all_recorded_yubikey_states
  test_remote_preflight_uses_scoped_sudo_command
  test_remote_detach_failure_is_reported
  test_custom_chrome_command_is_in_process_pattern
  test_chrome_command_rejects_shell_syntax
  test_existing_process_check_uses_custom_command
  test_existing_process_prompts_and_kills_by_default
  test_existing_process_declined_cancel_launch
  test_existing_process_allow_existing_skips_prompt
  test_existing_process_yes_skips_prompt
  test_find_busids_matches_usb_id
  test_internal_yubikey_run_sets_up_forwarding
  test_sysfs_detection_does_not_require_usbip
  test_readiness_wait_accepts_delayed_fido
  test_readiness_timeout_is_bounded
  test_invalid_timeout_values_fall_back
  test_readiness_probe_matches_exact_device_and_fido_metadata
  test_readiness_probe_rejects_unrelated_and_otp_hidraw_devices
  test_parent_wait_preserves_bootstrap_and_readiness_windows
  test_stop_cleans_provisional_state_without_busid
  test_stop_loads_earliest_usbipd_provisional_phase
  test_usbipd_state_write_failure_stops_before_daemon
  test_usbipd_state_write_records_pid_before_started_phase
  test_status_reports_yubikey_phase_without_cleanup
  test_status_reports_provisional_setup_lock
  test_status_without_host_reports_managed_overview
  test_attach_uses_tmux_context_appropriate_action
  test_subcommands_accept_help
  test_status_reports_legacy_state_truthfully
  test_stop_cleans_legacy_state_without_lock_or_token
  test_second_start_preserves_live_legacy_state
  test_local_missing_module_tree_has_diagnostics
  test_local_missing_module_has_diagnostics
  test_remote_preflight_failure_is_propagated
  test_other_exports_ignores_module_symlink
  test_launch_mode_auto_and_opt_out
  test_detached_duplicate_checks_tmux_before_existing_chrome
  test_foreground_duplicate_checks_yubikey_state_before_existing_chrome
  test_version_flag_reports_version
)

for test_name in "${tests[@]}"; do
  "$test_name"
  echo "PASS: $test_name"
done
