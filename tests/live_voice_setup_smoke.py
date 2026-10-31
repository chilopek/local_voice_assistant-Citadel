"""Prepare already-cached Whisper small through the actual settings IPC process."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

with tempfile.TemporaryDirectory(prefix='citadel-stt-settings-') as folder:
    path = Path(folder)/'assistant.local.json'
    path.write_text('{"voice":false,"model":"preserved","stt_model":"base"}',encoding='utf-8')
    env = dict(os.environ,CITADEL_DATA_DIR=folder,HF_HUB_OFFLINE='1')
    result = subprocess.run([sys.executable,'-B','-X','utf8','-m','assistant.voice_setup'],input='{"model":"small"}\n',
        capture_output=True,text=True,encoding='utf-8',env=env,timeout=120)
    assert result.returncode==0,(result.stdout,result.stderr)
    events = [json.loads(line) for line in result.stdout.splitlines()]
    assert any(item['event']=='ready' for item in events),events
    config = json.loads(path.read_text(encoding='utf-8'))
    assert config['stt_model']=='small' and config['model']=='preserved' and not config['voice'],config
    print('PASS: real settings process validates cached Whisper small offline before atomic config update; voice/provider preferences survive')
