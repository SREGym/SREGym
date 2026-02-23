import argparse
import subprocess
import sys


def read_commands(file_path):
    with open(file_path, "r") as f:
        lines = f.readlines()

    commands = []
    for line in lines:
        line = line.strip()

        # skip empty lines
        if not line:
            continue

        # skip comments like "# something"
        if line.startswith("#"):
            continue

        commands.append(line)

    return commands


def execute_commands(commands, step=False, dry_run=False, continue_on_error=False):
    total = len(commands)

    for i, cmd in enumerate(commands, start=1):
        print(f"\n[{i}/{total}] Running: {cmd}")

        # Step mode: wait until user presses Enter
        if step:
            input("Press Enter to continue...")

        # Dry-run: do not run, only show commands
        if dry_run:
            continue

        # run the command
        result = subprocess.run(cmd, shell=True)

        # if command fails
        if result.returncode != 0:
            print(f"Command failed with exit code {result.returncode}")
            if not continue_on_error:
                sys.exit(result.returncode)


def main():
    parser = argparse.ArgumentParser(description="Demo kubectl agent")
    parser.add_argument("--file", required=True, help="Path to kubectl_cmds.txt")
    parser.add_argument("--step", action="store_true", help="Step mode (press Enter)")
    parser.add_argument("--dry-run", action="store_true", help="Only print commands")
    parser.add_argument("--continue-on-error", action="store_true", help="Don't stop if a command fails")

    args = parser.parse_args()

    commands = read_commands(args.file)
    execute_commands(commands, step=args.step, dry_run=args.dry_run, continue_on_error=args.continue_on_error)


if __name__ == "__main__":
    main()