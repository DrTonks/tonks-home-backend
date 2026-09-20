"""Absolute runtime paths, independent of the process working directory."""
from dataclasses import dataclass
from pathlib import Path
import os
PROJECT_ROOT = Path(__file__).resolve().parents[1]

@dataclass(frozen=True)
class Paths:
    data_file: Path
    analytics_db: Path
    agent_db: Path
    recommendations_db: Path
    community_db: Path
    music: Path
    images: Path
    github_cache: Path
    article_manifest: Path

    @classmethod
    def from_env(cls):
        root = Path(os.environ.get('SLEEPY_DATA_DIR', str(PROJECT_ROOT))).expanduser().resolve()
        def value(key, default):
            p = Path(os.environ.get(key) or default).expanduser()
            return (p if p.is_absolute() else root / p).resolve()
        return cls(value('SLEEPY_DATA_FILE','data.json'), value('SLEEPY_ANALYTICS_DB','analytics.sqlite3'),
                   value('SLEEPY_AGENT_ACTIVITY_DB','agent_activity.sqlite3'), value('SLEEPY_RECOMMENDATIONS_DB','recommendations.sqlite3'),
                   value('SLEEPY_COMMUNITY_DB','community.sqlite3'), value('SLEEPY_MUSIC_DIR','music'),
                   value('SLEEPY_IMAGES_DIR','images'), value('SLEEPY_GITHUB_CACHE_FILE','github_stats_cache.json'),
                   value('SLEEPY_ARTICLE_MANIFEST','article-comments-manifest.json'))
