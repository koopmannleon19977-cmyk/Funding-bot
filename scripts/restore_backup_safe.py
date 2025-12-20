import zipfile
import os
import shutil

# Correct path to the backup zip
BACKUP_ZIP = r"c:\Users\koopm\funding-bot\backups\20251219_182154.zip"
TARGET_DIR = r"c:\Users\koopm\funding-bot"

def restore_backup(zip_path, target_dir):
    print(f"Starting resilient restore from: {zip_path}")
    print(f"Target directory: {target_dir}")
    
    if not os.path.exists(zip_path):
        print(f"ERROR: Backup file not found at {zip_path}")
        return

    skipped_files = []
    
    try:
        with zipfile.ZipFile(zip_path, 'r') as zip_ref:
            members = zip_ref.namelist()
            print(f"Found {len(members)} files/folders to restore.")
            
            for member in members:
                # Skip directories, extract method handles them, but we might want individual file control
                if member.endswith('/'):
                    continue
                
                target_path = os.path.join(target_dir, member)
                
                try:
                    # We use extract to a generic location? No, we extract individual file.
                    # zip_ref.extract(member, target_dir)
                    # To handle open files, we need to try/except per file.
                    
                    # Ensure dir exists
                    dir_name = os.path.dirname(target_path)
                    if dir_name and not os.path.exists(dir_name):
                        os.makedirs(dir_name, exist_ok=True)

                    # Read from zip and write to file
                    # This allows us to handle write errors gracefully
                    with zip_ref.open(member) as source, open(target_path, "wb") as target:
                        shutil.copyfileobj(source, target)
                        
                except Exception as e:
                    # Check if it's a permission error
                    error_msg = str(e)
                    if "Permission denied" in error_msg or "being used by another process" in error_msg or "Invalid argument" in error_msg:
                        print(f"SKIPPING LOCKED/ERROR file: {member} ({error_msg})")
                        skipped_files.append(member)
                    else:
                        print(f"Failed to extract {member}: {e}")
                        skipped_files.append(member)

            print("\nRestore process finished.")
            if skipped_files:
                print(f"WARNING: The following {len(skipped_files)} files were NOT restored (likely in use):")
                for f in skipped_files:
                    print(f" - {f}")
            else:
                print("SUCCESS: All files restored.")
            
    except Exception as e:
        print(f"CRITICAL ERROR during restore setup: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    restore_backup(BACKUP_ZIP, TARGET_DIR)
