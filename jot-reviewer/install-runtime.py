"""Install the locked reviewer runtime in its private, plugin-owned cache."""
import argparse
from pathlib import Path
import shutil
import subprocess


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dev', action='store_true', help='include type-checking dependencies')
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    runtime = root / '.venv'
    alias = root / 'node_modules'
    if alias.exists() and (not alias.is_symlink() or alias.resolve() != runtime / 'node_modules'):
        parser.error('existing node_modules is not managed by this installer; use a clean plugin checkout')
    runtime.mkdir(exist_ok=True)
    for name in ('package.json', 'package-lock.json'):
        shutil.copy2(root / name, runtime / name)
    command = ['npm', 'ci', '--prefix', str(runtime), '--ignore-scripts', '--no-audit', '--no-fund']
    if not args.dev:
        command.append('--omit=dev')
    subprocess.run(command, check=True, timeout=300)
    if not alias.is_symlink():
        alias.symlink_to(Path('.venv/node_modules'), target_is_directory=True)
    print('Reviewer runtime installed in .venv; gateway activation is a separate step.')


if __name__ == '__main__':
    main()
