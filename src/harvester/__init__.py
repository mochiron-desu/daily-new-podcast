"""News harvesting, deduplication, clustering, and dossier generation."""

from .dossier_builder import DossierArtifacts, build_dossier
from .rss_fetcher import NewsArticle, StoryCluster, cluster_articles, harvest_feeds

__all__ = [
    "DossierArtifacts",
    "NewsArticle",
    "StoryCluster",
    "build_dossier",
    "cluster_articles",
    "harvest_feeds",
]

