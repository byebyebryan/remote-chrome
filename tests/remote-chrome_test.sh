#!/usr/bin/env bash
# shellcheck disable=SC1091,SC2034,SC2154,SC2329
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

tests=(
  test_start_rolls_back_partial_setup
  test_successful_start_records_exact_state
  test_stop_cleans_only_recorded_busid
  test_remote_preflight_uses_scoped_sudo_command
)

for test_name in "${tests[@]}"; do
  "$test_name"
  echo "PASS: $test_name"
done
