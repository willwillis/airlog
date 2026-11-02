import os
from dataclasses import dataclass, field
from pathlib import Path

def flag(name, default='true'):
    return os.getenv(name, default).lower() in ('1', 'true', 'yes')

@dataclass
class Config:
    data: Path = field(default_factory=lambda: Path(os.getenv('DATA_DIR', '/data')))
    audio: Path = field(default_factory=lambda: Path(os.getenv('AUDIO_DIR', '/audio')))
    stream: str = field(default_factory=lambda: os.getenv('STREAM_URL', ''))
    stream_name: str = field(default_factory=lambda: os.getenv('STREAM_NAME', 'Internet Radio').strip() or 'Internet Radio')
    metadata_url: str = field(default_factory=lambda: os.getenv('METADATA_URL', ''))
    chunk_seconds: int = field(default_factory=lambda: int(os.getenv('CHUNK_SECONDS', '15')))
    sample_rate: int = 16000
    min_free_bytes: int = field(default_factory=lambda: int(float(os.getenv('MIN_FREE_GB', '5')) * 1_000_000_000))
    recognition: str = field(default_factory=lambda: os.getenv('RECOGNITION_PROVIDER', 'shazam'))
    request_interval: float = field(default_factory=lambda: float(os.getenv('RECOGNITION_INTERVAL_SECONDS', '10')))
    metadata_interval: int = field(default_factory=lambda: int(os.getenv('METADATA_INTERVAL_SECONDS', '15')))
    retention_hours: float = field(default_factory=lambda: float(os.getenv('MATCHED_RETENTION_HOURS', '1')))
    auto_delete: bool = field(default_factory=lambda: flag('AUTO_DELETE_MATCHED'))
    port: int = field(default_factory=lambda: int(os.getenv('PORT', '8080')))

    def prepare(self):
        if not 10 <= self.chunk_seconds <= 30:
            raise ValueError('CHUNK_SECONDS must be between 10 and 30')
        if self.recognition not in ('shazam', 'none'):
            raise ValueError('RECOGNITION_PROVIDER must be shazam or none')
        if self.request_interval < 1 or self.metadata_interval < 5 or self.retention_hours < 1 or self.min_free_bytes < 0:
            raise ValueError('Invalid interval, retention or disk reserve setting')
        for p in (self.data, self.audio, self.audio / 'queue', self.audio / 'capture'):
            p.mkdir(parents=True, exist_ok=True)
        return self
