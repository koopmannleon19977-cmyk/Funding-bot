import os
import zipfile

# Correct path to the backup zip
BACKUP_ZIP = r"c:\Users\koopm\funding-bot\backups\20251219_182154.zip"
TARGET_DIR = r"c:\Users\koopm\funding-bot"


def restore_backup(zip_path, target_dir):
    print(f"Starting restore from: {zip_path}")
    print(f"Target directory: {target_dir}")

    if not os.path.exists(zip_path):
        print(f"ERROR: Backup file not found at {zip_path}")
        return

    try:
        with zipfile.ZipFile(zip_path, "r") as zip_ref:
            # Extract all files
            print("Extracting files...")
            # We want to extract to target_dir.
            # Files in zip are relative to root (e.g. "config.py", "src/main.py")
            # This will overwrite existing files.

            # Count files
            file_count = len(zip_ref.namelist())
            print(f"Found {file_count} files/folders in archive.")

            zip_ref.extractall(target_dir)

            print("Restore completed successfully.")

    except Exception as e:
        print(f"CRITICAL ERROR during restore: {e}")
        import traceback

        traceback.print_exc()


if __name__ == "__main__":
    # Safety prompt bypass for automation, but usually we'd ask.
    # Since the user explicitly requested "restore the last backup", we proceed.
    restore_backup(BACKUP_ZIP, TARGET_DIR)
