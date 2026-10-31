"""Actual cached Silero eugene profile validation; no playback or real settings."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

with tempfile.TemporaryDirectory(prefix='citadel-profile-') as folder:
    path=Path(folder)/'assistant.local.json'
    path.write_text('{"model":"preserved","voice":false,"stt_model":"base"}')
    result=subprocess.run([sys.executable,'-B','-X','utf8','-m','assistant.voice_setup'],
        input='{"profile":"jarvis"}\n',capture_output=True,text=True,encoding='utf-8',
        env=dict(os.environ,CITADEL_DATA_DIR=folder,HF_HUB_OFFLINE='1'),timeout=120)
    assert result.returncode==0,(result.stdout,result.stderr)
    config=json.loads(path.read_text(encoding='utf-8'))
    assert config['tts_speaker']=='eugene' and config['tts_engine']=='silero' and config['voice_profile']=='jarvis',config
    assert config['model']=='preserved' and not config['voice'],config
    print('PASS: real eugene synthesis validated before atomic profile update; no playback or change to existing voice/model preference')
