"""Dependency wiring (one shared model provider, one registry, one orchestrator)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from app.bot.messenger import Messenger
from app.bot.service import BotService
from app.config import Settings
from app.execution.sandbox import Sandbox
from app.execution.workspace import WorkspaceManager
from app.orchestrator.budget import BudgetTracker
from app.orchestrator.project_manager import ProjectManager
from app.orchestrator.service import Orchestrator
from app.providers import build_provider
from app.providers.base import ModelProvider
from app.skills.discovery import SkillDiscoveryService
from app.skills.evaluator import SkillEvaluator
from app.skills.github_client import GitHubClient
from app.skills.importer import SkillImporter
from app.skills.registry import SkillRegistry
from app.storage.sqlite import SQLiteStorage


@dataclass
class App:
    settings: Settings
    storage: Any
    provider: ModelProvider
    registry: SkillRegistry
    pm: ProjectManager
    orchestrator: Orchestrator
    bot: BotService
    sandbox: Sandbox
    github: GitHubClient


def build_app(settings: Settings, messenger: Messenger, *, storage: Any = None, provider: Optional[ModelProvider] = None,
              github_transport=None) -> App:
    storage = storage or SQLiteStorage(settings.db_path)
    provider = provider or build_provider(settings)
    registry = SkillRegistry(settings.skills_dir)
    gh_kwargs: dict[str, Any] = {}
    if github_transport is not None:
        gh_kwargs["transport"] = github_transport
    github = GitHubClient(storage, token=settings.github_token, api_url=settings.github_api_url, **gh_kwargs)
    evaluator = SkillEvaluator(allow_copyleft=settings.allow_copyleft_skills)
    discovery = SkillDiscoveryService(github, evaluator, registry, storage, min_score=settings.skill_min_score)
    importer = SkillImporter(registry, github, storage, evaluator, discovery, trust_policy=settings.skill_trust_policy,
                             auto_min_score=settings.skill_auto_min_score, min_score=settings.skill_min_score,
                             allow_copyleft=settings.allow_copyleft_skills)
    pm = ProjectManager(provider, registry, settings, discovery=discovery, importer=importer, storage=storage)
    workspace = WorkspaceManager(settings.workspace_dir)
    sandbox = Sandbox(backend=settings.sandbox_backend, memory_mb=settings.sandbox_memory_mb,
                      timeout_s=settings.sandbox_timeout_s, allow_network=settings.sandbox_allow_network,
                      docker_image=settings.sandbox_docker_image)
    budget = BudgetTracker(settings)
    orch = Orchestrator(settings, storage, provider, pm, workspace, sandbox, messenger, budget)
    bot = BotService(settings, orch, registry, discovery, importer)
    return App(settings, storage, provider, registry, pm, orch, bot, sandbox, github)
