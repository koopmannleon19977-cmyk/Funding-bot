"""
Script to find unused Python files in the codebase.
"""
import ast
import os
from pathlib import Path
from typing import Set, Dict, List
from collections import defaultdict

def get_all_python_files(root_dir: str) -> Set[str]:
    """Get all Python files in the directory."""
    files = set()
    for root, dirs, filenames in os.walk(root_dir):
        # Skip common directories
        dirs[:] = [d for d in dirs if d not in ['__pycache__', '.git', 'node_modules', '.venv']]
        for filename in filenames:
            if filename.endswith('.py'):
                full_path = os.path.join(root, filename)
                # Normalize to module path
                rel_path = os.path.relpath(full_path, root_dir)
                module_path = rel_path.replace(os.sep, '.').replace('.py', '')
                files.add(module_path)
    return files

def extract_imports(file_path: str) -> Set[str]:
    """Extract all imports from a Python file."""
    imports = set()
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            tree = ast.parse(f.read(), filename=file_path)
        
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imports.add(alias.name.split('.')[0])
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    imports.add(node.module.split('.')[0])
    except Exception as e:
        print(f"Error parsing {file_path}: {e}")
    return imports

def find_unused_files(src_dir: str) -> Dict[str, List[str]]:
    """Find files that are never imported."""
    src_path = Path(src_dir)
    all_files = get_all_python_files(src_dir)
    
    # Files that are entry points (should not be checked)
    entry_points = {'src.main', 'src.core.startup', '__main__'}
    
    # Files that are imported
    imported_files = set()
    import_map = defaultdict(set)
    
    # Scan all Python files for imports
    for py_file in src_path.rglob('*.py'):
        if '__pycache__' in str(py_file):
            continue
        
        rel_path = py_file.relative_to(src_path)
        module_path = str(rel_path).replace(os.sep, '.').replace('.py', '')
        
        imports = extract_imports(str(py_file))
        for imp in imports:
            if imp.startswith('src.'):
                imported_files.add(imp)
                import_map[imp].add(module_path)
    
    # Find files that are never imported
    unused = []
    potentially_unused = []
    
    for file_module in all_files:
        if file_module in entry_points:
            continue
        
        # Check if this module is imported anywhere
        is_imported = False
        for imported in imported_files:
            if imported.startswith(file_module) or file_module.startswith(imported):
                is_imported = True
                break
        
        # Also check if it's a compatibility shim (these are intentionally kept)
        file_path = src_path / (file_module.replace('.', os.sep) + '.py')
        if file_path.exists():
            with open(file_path, 'r', encoding='utf-8') as f:
                content = f.read(200)  # Read first 200 chars
                if 'Compatibility shim' in content or '# Compatibility shim' in content:
                    continue  # Skip compatibility shims
        
        if not is_imported:
            # Check if it's a package __init__.py (might be used implicitly)
            if file_module.endswith('.__init__'):
                potentially_unused.append(file_module)
            else:
                unused.append(file_module)
    
    return {
        'unused': unused,
        'potentially_unused': potentially_unused,
        'imported': sorted(imported_files)
    }

if __name__ == '__main__':
    results = find_unused_files('src')
    
    print("=" * 80)
    print("UNUSED FILES ANALYSIS")
    print("=" * 80)
    print(f"\n📁 Total files analyzed: {len(results['unused']) + len(results['potentially_unused'])}")
    print(f"\n❌ Definitely unused ({len(results['unused'])}):")
    for f in sorted(results['unused']):
        print(f"  - {f}")
    
    print(f"\n⚠️  Potentially unused (empty __init__.py or package files) ({len(results['potentially_unused'])}):")
    for f in sorted(results['potentially_unused']):
        print(f"  - {f}")
    
    print(f"\n✅ Imported files: {len(results['imported'])}")
