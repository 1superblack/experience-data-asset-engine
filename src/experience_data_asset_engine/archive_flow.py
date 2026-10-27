"""归档准备：一次降噪，一份统计，所有分支共用。

``cli.py`` 负责参数解析与落盘，本模块只负责「拿到会话之后」的准备逻辑，
不 import ``argparse``，因此可以直接被测试与其它入口复用。

核心约束：**一次归档只跑一次 ``filter_noise``**。降噪结果（``cleaned``）与统计
（``NoiseReport``）在这里产生一次，随后无论是 ``--distilled`` 覆盖统计、交给模型蒸馏、
还是写 staging 请求，用的都是同一份——不存在「笔记里一套、日志里另一套」的可能。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

from . import noise as noise_module
from .config import AppConfig
from .distill import DistillationRequired, prepare_distillation
from .errors import ConfigError
from .models import DistilledNote, NoiseReport, NormalizedSession

MODE_DISTILLED = "distilled"
MODE_PROVIDER = "provider"
MODE_PROVIDER_PLANNED = "provider-planned"
MODE_STAGING = "staging"
MODE_STAGING_PLANNED = "staging-planned"


@dataclass(frozen=True, slots=True)
class ArchivePlan:
    """一次归档的准备工作结果（真跑与 dry-run 共用同一套准备逻辑）。

    - ``session``：原始会话，逐字未改，用于 ``content_hash`` 与 state_key；
    - ``cleaned``：降噪后的派生会话（原对象不动），staging 与模型提示词都用它；
    - ``report``：唯一统计口径，正式笔记与 pending 的「降噪统计」章节都取它；
    - ``mode``：``distilled`` / ``provider`` / ``provider-planned`` /
      ``staging`` / ``staging-planned``。
    """

    session: NormalizedSession
    cleaned: NormalizedSession
    report: NoiseReport
    mode: str
    note: DistilledNote | None = None
    staging_path: Path | None = None
    schema_path: Path | None = None

    @property
    def staging_required(self) -> bool:
        return self.mode in {MODE_STAGING, MODE_STAGING_PLANNED}

    @property
    def provider_planned(self) -> bool:
        return self.mode == MODE_PROVIDER_PLANNED

    @property
    def distilled(self) -> bool:
        return self.mode == MODE_DISTILLED


def prepare_archive(
    session: NormalizedSession,
    config: AppConfig,
    *,
    distilled_note: DistilledNote | None = None,
    allow_side_effects: bool = True,
) -> ArchivePlan:
    """解析完成之后的归档准备。

    ``allow_side_effects=False`` 是 dry-run 语义：不调用模型、不写 staging，
    但统计、笔记覆盖与 ``content_hash`` 与真跑完全一致。
    """
    cleaned, report = noise_module.filter_noise(session)

    if distilled_note is not None:
        # 外部 JSON 自带的统计一律丢弃：只认管道这一次的真实结果
        return ArchivePlan(
            session=session,
            cleaned=cleaned,
            report=report,
            mode=MODE_DISTILLED,
            note=replace(distilled_note, noise_report=report),
        )

    if config.provider:
        if not allow_side_effects:
            return ArchivePlan(
                session=session,
                cleaned=cleaned,
                report=report,
                mode=MODE_PROVIDER_PLANNED,
            )
        note = prepare_distillation(session, config, filtered=(cleaned, report))
        # 统计用蒸馏内部那一份（就是上面这份 report），这里不重算
        return ArchivePlan(
            session=session,
            cleaned=cleaned,
            report=note.noise_report,
            mode=MODE_PROVIDER,
            note=note,
        )

    if not allow_side_effects:
        return ArchivePlan(
            session=session,
            cleaned=cleaned,
            report=report,
            mode=MODE_STAGING_PLANNED,
        )

    try:
        prepare_distillation(session, config, filtered=(cleaned, report))
    except DistillationRequired as exc:
        return ArchivePlan(
            session=session,
            cleaned=cleaned,
            report=report,
            mode=MODE_STAGING,
            staging_path=exc.staging_path,
            schema_path=exc.schema_path,
        )
    raise ConfigError("内部状态异常：无 provider 时不应直接返回笔记")
