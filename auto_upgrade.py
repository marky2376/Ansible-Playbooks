import time
import datetime
import os
import difflib
from netmiko import ConnectHandler
from twilio.rest import Client
import logging
import paramiko
import re
import argparse

#logging.basicConfig(level=logging.DEBUG)
#paramiko.util.log_to_file("paramiko_debug.log")

rad_username = os.getenv('USERNAME')
rad_password = os.getenv('PASSWORD')


TWILIO_SID = os.getenv('TWILIO_SID')
TWILIO_AUTH_TOKEN = os.getenv('TWILIO_AUTH_TOKEN')
TWILIO_FROM_NUMBER = os.getenv('TWILIO_FROM_NUMBER')
EMERGENCY_PHONE_NUMBER = '+18033792484'

TIME_REGEX = re.compile(r'^\d{2}:\d{2}:\d{2}$') i

def parse_args():
    parser = argparse.ArgumentParser(description="Router Upgrade Script")
    parser.add_argument("--router_ip", required=True, help="Router IP address")
    parser.add_argument("--emergency_phone", required=False,
                        default=EMERGENCY_PHONE_NUMBER,
                        help="Emergency phone number (in E.164 format)")
    return parser.parse_args()


def wait_for_call_status(client, call_sid, poll_interval=5):
    """
    Polls Twilio until the call is in a final status.
    Returns the final status as a string:
    'completed', 'no-answer', 'busy', 'failed', 'canceled', etc.
    """
    final_statuses = {"completed", "no-answer", "busy", "failed", "canceled"}
    while True:
        call_obj = client.calls(call_sid).fetch()
        current_status = call_obj.status
        if current_status in final_statuses:
            return current_status
        time.sleep(poll_interval)

def call_until_human_amd(
    max_retries=3,
    retry_delay=30,
    ring_timeout=60,
    message="Emergency: Router Issue Detected!"
):
    """
    Places a call via Twilio with AMD enabled. It retries up to max_retries
    if the call is not answered by a human.
    """
    client = Client(TWILIO_SID, TWILIO_AUTH_TOKEN)

    for attempt in range(1, max_retries + 1):
        print(f"[TWILIO] AMD call attempt {attempt} of {max_retries}...")

        # Initiate the call with AMD enabled. A simple TwiML message is used.
        call = client.calls.create(
            to=EMERGENCY_PHONE_NUMBER,
            from_=TWILIO_FROM_NUMBER,
            twiml=f'<Response><Say voice="alice">{message}</Say></Response>',
            timeout=ring_timeout,  # how long to ring before no-answer
            machine_detection="Enable"  # Enable Answering Machine Detection
        )

        print(f"[TWILIO] Call initiated, SID = {call.sid}. Waiting for final status...")

        # Wait for the call to finish
        final_status = wait_for_call_status(client, call.sid)
        print(f"[TWILIO] Final status: {final_status}")

        # Re-fetch the call details to check the 'answered_by' field.
        call_obj = client.calls(call.sid).fetch()
        answered_by = call_obj.answered_by  # Expected values: "human", "machine", etc.
        print(f"[TWILIO] answered_by: {answered_by}")

        # If the call is completed and answered_by indicates a human, then stop.
        if final_status == "completed" and answered_by == "human":
            print("[TWILIO] Call was answered by a HUMAN!")
            return
        else:
            print("[TWILIO] Call was not answered by a human. Retrying...")

        if attempt < max_retries:
            print(f"[TWILIO] Retrying in {retry_delay} seconds...")
            time.sleep(retry_delay)
        else:
            print("[TWILIO] Reached max retries without a human answer.")


def poll_router(router_info, commands):
    """
    Polls the router with the given commands and returns the filename
    of the file containing the output.
    """
    # Connect to the router
    print(f"Connecting to {router_info['ip']}...")
    try:
        net_connect = ConnectHandler(**router_info)
    except Exception as e:
        print(f"[ERROR] Could not connect to {router_info['ip']}: {e}")
        return False, None
    print("Connected.")

    # Capture output for each command
    results = {}
    for cmd in commands:
        print(f"Running command: {cmd}")
        results[cmd] = net_connect.send_command(cmd)
        time.sleep(1)  # Slight delay between commands if needed

    # Disconnect
    net_connect.disconnect()
    print("Disconnected from the router.")

    # Create a timestamped filename
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"router_output_{timestamp}.txt"

    # Write all command outputs to the file
    with open(filename, "w") as f:
        for cmd, output in results.items():
            f.write(f"==== Command: {cmd} ====\n")
            f.write(output + "\n\n")

    print(f"Output saved to {filename}")
    return True, filename

def normalize_bgp_summary_lines(lines):

    normalized = []
    for line in lines:

        cols = line.split()

        if len(cols) >= 10:
            # We'll guess that:
            #  cols[0] -> Neighbor IP
            #  cols[8] -> Up/Down
            #  cols[9] -> St/PfxRcd (or State/PfxRcd)

            neighbor = cols[0]
            up_down = cols[8]
            st_pfxrcd = cols[9]

            # If Up/Down is just a time, replace it
            if TIME_REGEX.match(up_down):
                up_down = "UPTIME"  # or "TIME"

            # Reassemble a simplified line for the diff
            # For example: "10.10.10.1 UPTIME 0"
            new_line = f"{neighbor} {up_down} {st_pfxrcd}"
            normalized.append(new_line + "\n")  # Keep newline for diffs
        else:
            # If not a neighbor line, just keep it as-is
            normalized.append(line)
    return normalized

def omit_bgp_metadata(lines):
    """
    Returns a list of lines, omitting any line that contains
    known BGP metadata strings you don't want to see in diffs.
    """
    skip_substrings = [
        "Table ID:",
        "BGP main routing table version",
        "BGP NSR",
        "RD version:",
        "Speaker",
        "BGP table nexthop route policy:",
        "!!",
        "service unsupported-transceiver",
        "ssh server v2",
        "telnet vrf default ipv4"
    ]
    filtered = []
    for line in lines:
        # If line contains *any* of the substrings, skip it
        if any(s in line for s in skip_substrings):
            continue
        filtered.append(line)
    return filtered

def normalize_timestamps(lines):
    """
    Returns a list of lines where certain time/datetime patterns
    are replaced with a fixed token 'TIME' (or just removed).
    """
    # Example regex patterns that might match times like:
    #  "20:19:13.918" or "20:29:20.350" or "00:11:26" or "00:04:07"
    # You can adjust these to match your device's timestamps more precisely.
    time_pattern = re.compile(r"\b\d{2}:\d{2}:\d{2}(\.\d+)?\b")  # HH:MM:SS(.ddd)?
    date_pattern = re.compile(r"\b[A-Z][a-z]{2}\s[A-Z][a-z]{2}\s\d{1,2}\s\d{2}:\d{2}:\d{2}\.\d+\sUTC\b")
    # Above tries to match lines like "Wed Jan 22 20:29:20.350 UTC"

    normalized = []
    for line in lines:
        # Replace date/time occurrences with a placeholder
        line = date_pattern.sub("DATE_TIME", line)
        line = time_pattern.sub("TIME", line)
        # Also remove "Neighbor is up for 00:xx:xx" times
        line = re.sub(r"(Neighbor is up for )\d{2}:\d{2}:\d{2}", r"\1TIME", line)
        normalized.append(line)
    return normalized

def compare_files(file1, file2, diff_threshold=1):
    with open(file1, "r") as f1, open(file2, "r") as f2:
        old_lines = f1.readlines()
        new_lines = f2.readlines()

    # -- OLD FILE LINES --
    old_lines = normalize_timestamps(old_lines)     # Step 1
    old_lines = omit_bgp_metadata(old_lines)        # Step 2
    old_lines = normalize_bgp_summary_lines(old_lines)  # Step 3

    # -- NEW FILE LINES --
    new_lines = normalize_timestamps(new_lines)     # Step 1
    new_lines = omit_bgp_metadata(new_lines)        # Step 2
    new_lines = normalize_bgp_summary_lines(new_lines)  # Step 3

    diff = list(difflib.unified_diff(
        old_lines,
        new_lines,
        fromfile=file1,
        tofile=file2
    ))

    significant_change = (len(diff) > diff_threshold)
    return diff, significant_change

def install_activate(router_info):
    """
    This function connects to the router, runs 'install activate',
    waits 5 seconds, then sends an extra newline to continue.
    Adjust as needed if the device prompts for 'yes/no'.
    """
    print("Connecting to router for install activate...")
    try:
        net_connect = ConnectHandler(**router_info)
        print("Connected. Sending 'install activate'...")
        # Use send_command_timing if you expect a prompt that needs a quick response
        output = net_connect.send_command_timing("install activate")

        # Wait 5 seconds
        time.sleep(5)

        output += net_connect.send_command_timing("")

        net_connect.disconnect()
        print("Install activate command completed. Output was:")
        print(output)
    except Exception as e:
        print(f"[ERROR] Could not run 'install activate' on {router_info['ip']}: {e}")


def main():
    args = parse_args()
    # Override global variables with command-line arguments
    global EMERGENCY_PHONE_NUMBER
    EMERGENCY_PHONE_NUMBER = args.emergency_phone

    # Router connection info
    router_info = {
        'device_type': 'cisco_xr',  # Netmiko device type for IOS-XR
        'ip': args.router_ip,       # Replace with your router's IP
        'username': rad_username,        # Replace with your username
        'ssh_strict': False,
        'system_host_keys': False,
        'use_keys': False,
        'allow_agent': False,
        'ssh_config_file': None,
        'password': rad_password,     # Replace with your password
        #'secret': '',               # If needed for IOS-XR
    }

    # List of show commands you want to run
    commands = [
        "show bgp summ",
        "show ospf nei",
        "show ipv4 int brief",
        "show run"
        # Add more commands as needed
    ]

    # How long to wait between polls (in seconds)
    poll_interval = 1200  # e.g., 60 seconds = 1 minute

    print("Running Pre Upgrade show commands...")
    success, first_output_file = poll_router(router_info, commands)
    if not success:
        # Router is unreachable on the first poll
        call_until_human_amd(
            max_retries=5,
            retry_delay=30,
            ring_timeout=45
        )

        return

    print("Activating Upgrade........")
    install_activate(router_info)

    print(f"Waiting {poll_interval} seconds before the next poll...\n")
    time.sleep(poll_interval)

    print("Running Post Upgrade show commands for diff merge...")
    success, second_output_file = poll_router(router_info, commands)
    if not success:
       # Router is not reachable on second attempt
        call_until_human_amd(
            max_retries=5,
            retry_delay=30,
            ring_timeout=45
        )

        return

    print("Comparing Pre and Post upgrade files...")
    diff, significant_change = compare_files(first_output_file, second_output_file, diff_threshold=10)

    if diff:
        print("\nDifferences found:\n")
        for line in diff:
            print(line, end="")  # diffs already contain newlines

        # If the difference is significant, call the emergency line
        if significant_change:
            call_until_human_amd(
                max_retries=5,
                retry_delay=30,
                ring_timeout=45
            )
    else:
        print("No differences found between the two outputs.")

if __name__ == "__main__":
    main()
