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
  assert_contains "$events" "detach:5-1.2.2"
  assert_contains "$events" "close-tunnel"
  assert_contains "$events" "sudo:usbip unbind -b 5-1.2.2"
  assert_contains "$events" "stop-daemon"
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

test_remote_preflight_uses_scoped_sudo_command() (
  yk_remote="test-host"
  local remote_command=""

  yk_ssh() {
    remote_command="$*"
    return 0
  }

  yk_ensure_remote_ready
  assert_contains "$remote_command" "sudo -n modprobe vhci-hcd"
  [[ "$remote_command" != *"sudo -n true"* ]] ||
    fail "remote preflight still requires unrestricted sudo via true"
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

test_version_flag_reports_version() (
  local output
  output="$(main --version)"
  assert_contains "$output" "$VERSION"
  [[ "$output" == *"$PROGRAM"* ]] || fail "version output omitted the program name"
)

tests=(
  test_start_rolls_back_partial_setup
  test_successful_start_records_exact_state
  test_stop_cleans_only_recorded_busid
  test_stop_cleans_yubikey_without_yubikey_window
  test_remote_preflight_uses_scoped_sudo_command
  test_custom_chrome_command_is_in_process_pattern
  test_chrome_command_rejects_shell_syntax
  test_existing_process_check_uses_custom_command
  test_existing_process_prompts_and_kills_by_default
  test_existing_process_declined_cancel_launch
  test_existing_process_allow_existing_skips_prompt
  test_existing_process_yes_skips_prompt
  test_find_busids_matches_usb_id
  test_internal_yubikey_run_sets_up_forwarding
  test_version_flag_reports_version
)

for test_name in "${tests[@]}"; do
  "$test_name"
  echo "PASS: $test_name"
done
