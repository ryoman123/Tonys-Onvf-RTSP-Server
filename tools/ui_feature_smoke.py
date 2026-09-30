#!/usr/bin/env python3
"""Render inherited pages and execute the real browser profile selector."""
from pathlib import Path
import re
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.web_template import get_web_ui_html, get_login_html, get_setup_html


def main():
    html = get_web_ui_html()
    with tempfile.TemporaryDirectory() as directory:
        for page_index, page in enumerate((html, get_login_html(), get_setup_html())):
            for index, script in enumerate(re.findall(r'<script(?:\s[^>]*)?>(.*?)</script>', page, re.S)):
                if not script.strip():
                    continue
                path = Path(directory) / f'{page_index}-{index}.js'
                path.write_text(script)
                subprocess.run(['node', '--check', str(path)], check=True, capture_output=True, text=True)
        start = html.index('function browserStreamPath(')
        end = html.index('async function initWebRTCPlayer(', start)
        helper = html[start:end]
        script = """
const assert = require('node:assert/strict');
const cameras = [{id: 1, browserPaths: {main: 'native_main_browser', sub: 'native_sub_browser'}}];
const settings = {};
const matrixStreamProfiles = {};
""" + helper + """
assert.equal(browserStreamPath('player-1', 1, 'native'), 'native_sub_browser');
matrixStreamProfiles[1] = 'main';
assert.equal(browserStreamPath('matrix-player-1', '1', 'native'), 'native_main_browser');
matrixStreamProfiles[1] = 'sub';
settings.matrixForceHighStream = true;
assert.equal(browserStreamPath('matrix-player-1', 1, 'native'), 'native_main_browser');
assert.equal(browserStreamPath('player-2', 2, 'other'), 'other_sub');
"""
        subprocess.run(['node', '-e', script], check=True, capture_output=True, text=True)
    print('PASS: rendered page scripts and actual browser main/sub selection')


if __name__ == '__main__':
    main()
