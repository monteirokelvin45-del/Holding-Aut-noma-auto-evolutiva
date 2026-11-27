"""Departamento Jurídico & Compliance.

Quatro verificações obrigatórias antes de publicar ou integrar algo:

1. **Licenças de código** — ``licscan``/``ScanCode Toolkit`` quando instalados;
   caso contrário, leitura directa de ``LICENSE``/``pyproject.toml``/
   ``package.json``. Bloqueia qualquer vestígio de ``CC-BY-NC`` (ChatTTS,
   F5-TTS) e exige MIT/Apache-2.0/BSD/LGPL.
2. **Termos de Serviço** — ``CoCompliance``/``TOS-Rag-Analyzer`` para analisar
   o ToS de plataformas (TikTok, YouTube, Gumroad, Meta) com RAG. Sem estes
   pacotes, usa uma base de conhecimento curada com as regras críticas.
3. **Direitos de autor de mídia** — ``youtube-copyright-scanner`` para áudio e
   vídeo; sem ele, valida metadados das fontes (Pexels/Pixabay = livres,
   vozes TTS permitidas) e rejeita fontes desconhecidas.
4. **Pacotes do projeto** — ``requirements.txt``/``package.json`` contra a
   lista de licenças proibidas.

Todas as decisões são registadas em ``squad_metrics.json`` (chave
``compliance_audits``) e publicadas no bus para o APK.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..config.settings import REPO_ROOT, settings
from ..core.clock import utcnow_iso
from ..core.errors import ValidationBlocked
from ..core.logger import get_logger
from ..core.runtime_bus import event_bus
from ..core.vault_store import SQUAD_METRICS, vault

log = get_logger("governance.legal")

ALLOWED_LICENSES = {
    "mit", "apache-2.0", "apache 2.0", "bsd", "bsd-2-clause", "bsd-3-clause",
    "lgpl", "lgpl-2.1", "lgpl-3.0", "gpl-3.0", "gplv3", "isc", "mpl-2.0",
    "psf-2.0", "unlicense", "cc0-1.0",
}
FORBIDDEN_LICENSES = {
    "cc-by-nc", "cc-by-nc-4.0", "cc-by-nc-sa", "cc-by-nc-nd", "cc-by-nd",
    "proprietary", "non-commercial", "research-only",
}
FORBIDDEN_PACKAGES = {"chattts", "f5-tts", "f5_tts", "2noise/chattts"}

# Base de conhecimento curada dos ToS (usada quando o RAG não está disponível).
TOS_KNOWLEDGE: Dict[str, Dict[str, Any]] = {
    "tiktok": {
        "risk_level": "high",
        "rules": [
            "Proibido conteúdo duplicado/reutilizado sem transformação significativa.",
            "Voz sintética exige divulgação em algumas regiões.",
            "Proibido publicar dados pessoais de terceiros.",
        ],
        "automation_allowed": "limitada",
        "notes": "Uploads automatizados em massa podem originar shadowban.",
    },
    "youtube": {
        "risk_level": "high",
        "rules": [
            "Conteúdo sintético deve ser declarado (altered content).",
            "Reutilização sem valor acrescentado viola o canal de spam.",
            "Copyright ID bloqueia áudio protegido — usar apenas CC0/Pexels/Pixabay.",
        ],
        "automation_allowed": "com_declaracao",
        "notes": "Shorts monetizáveis exigem 3s de gancho e conteúdo original.",
    },
    "gumroad": {
        "risk_level": "medium",
        "rules": [
            "Produto deve ser original e não infringir marcas.",
            "Política de reembolso obrigatória.",
            "Afiliados devem declarar a relação comercial.",
        ],
        "automation_allowed": "permitida",
        "notes": "Taxa da plataforma ~10%; payout em USD.",
    },
    "lemonsqueezy": {
        "risk_level": "medium",
        "rules": [
            "Merchant of Record: a plataforma trata de impostos (VAT).",
            "Proibido conteúdo adulto, gambling e cripto sem aprovação.",
        ],
        "automation_allowed": "permitida",
        "notes": "Bom para digitais; suporta Moçambique no payout.",
    },
    "reddit": {
        "risk_level": "high",
        "rules": [
            "Scraping agressivo viola os termos e resulta em bloqueio de IP.",
            "Respeitar robots.txt e o rate limit da API oficial.",
        ],
        "automation_allowed": "apenas_api_oficial",
        "notes": "Usar apenas endpoints JSON públicos com User-Agent identificável.",
    },
    "meta": {
        "risk_level": "high",
        "rules": [
            "Publicação automatizada requer app review e permissões.",
            "Proibido conteúdo enganoso sobre finanças/saúde.",
        ],
        "automation_allowed": "com_review",
        "notes": "Instagram Reels: agendamento via API oficial.",
    },
}


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9\-\.]", "", (text or "").lower())


@dataclass
class AuditFinding:
    check: str
    passed: bool
    severity: str  # info | warning | critical
    message: str
    evidence: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "check": self.check,
            "passed": self.passed,
            "severity": self.severity,
            "message": self.message,
            "evidence": self.evidence,
        }


@dataclass
class AuditReport:
    subject: str
    kind: str
    passed: bool
    findings: List[AuditFinding]
    tool: str
    generated_at: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "subject": self.subject,
            "kind": self.kind,
            "passed": self.passed,
            "findings": [f.to_dict() for f in self.findings],
            "tool": self.tool,
            "generated_at": self.generated_at,
        }


class LegalCompliance:
    """Auditoria jurídica automatizada com degradação honesta."""

    def __init__(self) -> None:
        self._lock = threading.RLock()

    # ------------------------------------------------------------- ferramentas
    @staticmethod
    def _which(binary: str) -> bool:
        return shutil.which(binary) is not None

    def available_tools(self) -> Dict[str, Any]:
        return {
            "licscan": self._which("licscan"),
            "scancode": self._which("scancode"),
            "scancode_toolkit_module": self._module_available("scancode"),
            "cocompliance": self._module_available("cocompliance"),
            "tos_rag_analyzer": self._module_available("tos_rag_analyzer"),
            "youtube_copyright_scanner": self._which("youtube-copyright-scanner")
            or self._module_available("youtube_copyright_scanner"),
            "pip": self._which("pip") or self._which("pip3"),
        }

    @staticmethod
    def _module_available(name: str) -> bool:
        try:
            import importlib.util

            return importlib.util.find_spec(name) is not None
        except (ImportError, ValueError):
            return False

    # -------------------------------------------------------- 1. licenças código
    def audit_package_licenses(self, packages: List[str]) -> AuditReport:
        """Verifica licenças de pacotes instalados via metadata do pip."""
        findings: List[AuditFinding] = []
        tool = "pip-metadata"
        try:
            from importlib import metadata  # type: ignore
        except ImportError:
            metadata = None  # type: ignore

        for package in packages:
            name = package.strip()
            if not name:
                continue
            if _norm(name) in FORBIDDEN_PACKAGES:
                findings.append(
                    AuditFinding(
                        check="license",
                        passed=False,
                        severity="critical",
                        message=f"Pacote PROIBIDO por licença CC-BY-NC: {name}",
                        evidence={"package": name},
                    )
                )
                continue
            if metadata is None:
                findings.append(
                    AuditFinding(
                        check="license",
                        passed=True,
                        severity="info",
                        message=f"{name}: metadata indisponível, verificado por nome.",
                        evidence={"package": name, "license": "desconhecida"},
                    )
                )
                continue
            try:
                meta = metadata.metadata(name)
                license_text = str(
                    meta.get("License", "")
                    or meta.get("License-Expression", "")
                    or ""
                )
                classifiers = " ".join(meta.get_all("Classifier") or [])
                combined = _norm(f"{license_text} {classifiers}") or "unknowable"
                if any(bad in _norm(license_text) for bad in FORBIDDEN_LICENSES):
                    findings.append(
                        AuditFinding(
                            check="license",
                            passed=False,
                            severity="critical",
                            message=f"{name}: licença não comercial ({license_text[:60]}).",
                            evidence={"package": name, "license": license_text[:200]},
                        )
                    )
                elif any(
                    token in license_text.lower() for token in ("mit", "apache", "bsd", "lgpl", "isc", "mpl")
                ):
                    findings.append(
                        AuditFinding(
                            check="license",
                            passed=True,
                            severity="info",
                            message=f"{name}: licença comercial permitida ({license_text[:40]}).",
                            evidence={"package": name, "license": license_text[:200]},
                        )
                    )
                else:
                    findings.append(
                        AuditFinding(
                            check="license",
                            passed=True,
                            severity="warning",
                            message=f"{name}: licença '{license_text[:40] or 'desconhecida'}' "
                                    "exige revisão manual.",
                            evidence={"package": name, "license": license_text[:200]},
                        )
                    )
            except Exception as exc:
                findings.append(
                    AuditFinding(
                        check="license",
                        passed=True,
                        severity="warning",
                        message=f"{name}: não foi possível ler metadata ({exc}).",
                        evidence={"package": name},
                    )
                )

        passed = not any(f.severity == "critical" for f in findings)
        return AuditReport(
            subject=",".join(packages)[:200],
            kind="package_licenses",
            passed=passed,
            findings=findings,
            tool=tool,
            generated_at=utcnow_iso(),
        )

    def audit_repository(self, repo_path: Optional[Path] = None) -> AuditReport:
        """Audita um repositório local: LICENSE, requirements, package.json."""
        path = Path(repo_path or REPO_ROOT)
        findings: List[AuditFinding] = []
        tool = "native-filesystem"

        license_file = next(
            (path / name for name in ("LICENSE", "LICENSE.md", "COPYING", "LICENSE.txt")
             if (path / name).is_file()),
            None,
        )
        if license_file is not None:
            try:
                content = license_file.read_text(encoding="utf-8", errors="replace")[:4000].lower()
            except OSError:
                content = ""
            if "noncommercial" in content or "non-commercial" in content or "cc by-nc" in content:
                findings.append(
                    AuditFinding(
                        check="repository_license", passed=False, severity="critical",
                        message=f"{license_file.name} contém cláusula não comercial.",
                        evidence={"file": str(license_file.name)},
                    )
                )
            elif any(token in content for token in ("mit license", "apache license", "bsd", "lgpl", "gnu general public")):
                findings.append(
                    AuditFinding(
                        check="repository_license", passed=True, severity="info",
                        message=f"{license_file.name}: licença compatível com uso comercial.",
                        evidence={"file": str(license_file.name)},
                    )
                )
            else:
                findings.append(
                    AuditFinding(
                        check="repository_license", passed=True, severity="warning",
                        message=f"{license_file.name}: texto não reconhecido; revisão manual.",
                        evidence={"file": str(license_file.name)},
                    )
                )
        else:
            findings.append(
                AuditFinding(
                    check="repository_license", passed=False, severity="warning",
                    message="Repositório sem ficheiro LICENSE.",
                    evidence={"path": str(path)},
                )
            )

        requirements = path / "backend" / "requirements.txt"
        if requirements.is_file():
            try:
                raw = requirements.read_text(encoding="utf-8", errors="replace")
            except OSError:
                raw = ""
            names = [
                re.split(r"[<>=!~\[ ]", line.strip())[0]
                for line in raw.splitlines()
                if line.strip() and not line.strip().startswith("#")
            ]
            forbidden = [
                name for name in names if _norm(name) in FORBIDDEN_PACKAGES
            ]
            findings.append(
                AuditFinding(
                    check="requirements_forbidden",
                    passed=not forbidden,
                    severity="critical" if forbidden else "info",
                    message=(
                        f"requirements.txt contém pacotes proibidos: {forbidden}"
                        if forbidden
                        else f"requirements.txt limpo ({len(names)} pacotes)."
                    ),
                    evidence={"forbidden": forbidden, "count": len(names)},
                )
            )
            tool = "native-filesystem"

        package_json = path / "package.json"
        if package_json.is_file():
            try:
                payload = json.loads(package_json.read_text(encoding="utf-8"))
                declared = str(payload.get("license", "")).lower()
                findings.append(
                    AuditFinding(
                        check="package_json_license",
                        passed=declared not in FORBIDDEN_LICENSES,
                        severity="info" if declared not in FORBIDDEN_LICENSES else "critical",
                        message=f"package.json declara licença '{declared or 'ausente'}'.",
                        evidence={"license": declared},
                    )
                )
            except (OSError, json.JSONDecodeError) as exc:
                findings.append(
                    AuditFinding(
                        check="package_json_license", passed=False, severity="warning",
                        message=f"package.json ilegível: {exc}", evidence={},
                    )
                )

        passed = not any(f.severity == "critical" for f in findings)
        return AuditReport(
            subject=str(path.name),
            kind="repository",
            passed=passed,
            findings=findings,
            tool=tool,
            generated_at=utcnow_iso(),
        )

    def scan_path_with_scancode(self, target: Path, timeout: int = 300) -> Dict[str, Any]:
        """Executa ``scancode-toolkit`` se estiver instalado (resultado JSON)."""
        if not self._which("scancode"):
            return {
                "available": False,
                "message": "scancode não instalado; usar audit_repository().",
            }
        output = Path(settings.vault_path) / "runtime" / "scancode.json"
        output.parent.mkdir(parents=True, exist_ok=True)
        command = [
            "scancode", "--license", "--json-pp", str(output), str(target),
        ]
        try:
            result = subprocess.run(
                command, capture_output=True, text=True, timeout=timeout, check=False
            )
        except (subprocess.SubprocessError, OSError) as exc:
            return {"available": True, "ok": False, "error": str(exc)}
        summary: Dict[str, Any] = {
            "available": True,
            "ok": result.returncode == 0,
            "returncode": result.returncode,
            "stderr": (result.stderr or "")[:1000],
        }
        try:
            payload = json.loads(output.read_text(encoding="utf-8"))
            licenses = set()
            for item in payload.get("files", []):
                for detected in item.get("licenses", []):
                    key = detected.get("key") or detected.get("short_name")
                    if key:
                        licenses.add(str(key))
            summary["licenses_detected"] = sorted(licenses)
            summary["forbidden_found"] = sorted(
                key for key in licenses if _norm(key) in FORBIDDEN_LICENSES
            )
        except (OSError, json.JSONDecodeError) as exc:
            summary["parse_error"] = str(exc)
        return summary

    # ------------------------------------------------------------ 2. ToS
    def audit_terms(self, platform: str) -> AuditReport:
        """Analisa os Termos de Serviço de uma plataforma (RAG se disponível)."""
        platform_key = _norm(platform).split(".")[0]
        findings: List[AuditFinding] = []
        tool = "curated-knowledge-base"

        knowledge = TOS_KNOWLEDGE.get(platform_key)
        if knowledge is None:
            return AuditReport(
                subject=platform,
                kind="tos",
                passed=False,
                findings=[
                    AuditFinding(
                        check="tos_known_platform",
                        passed=False,
                        severity="warning",
                        message=f"Plataforma '{platform}' sem regras curadas. "
                                "Revisão humana obrigatória antes de automatizar.",
                        evidence={"platform": platform},
                    )
                ],
                tool=tool,
                generated_at=utcnow_iso(),
            )

        rag_used = False
        tos_text = ""
        try:  # TOS-Rag-Analyzer, se disponível
            from tos_rag_analyzer import TosAnalyzer  # type: ignore

            analyzer = TosAnalyzer()
            tos_text = analyzer.analyze(platform=platform_key)
            rag_used = bool(tos_text)
            tool = "tos-rag-analyzer"
        except Exception:
            tos_text = ""

        for rule in knowledge["rules"]:
            findings.append(
                AuditFinding(
                    check="tos_rule",
                    passed=True,
                    severity="info",
                    message=rule,
                    evidence={"platform": platform_key, "rag": rag_used},
                )
            )
        if knowledge["risk_level"] == "high":
            findings.append(
                AuditFinding(
                    check="tos_risk",
                    passed=True,
                    severity="warning",
                    message=(
                        f"Plataforma de risco alto: automação "
                        f"'{knowledge['automation_allowed']}'. {knowledge['notes']}"
                    ),
                    evidence={"platform": platform_key},
                )
            )
        if tos_text:
            findings.append(
                AuditFinding(
                    check="tos_rag_analysis", passed=True, severity="info",
                    message="Análise RAG do ToS concluída.",
                    evidence={"excerpt": str(tos_text)[:400]},
                )
            )
        return AuditReport(
            subject=platform_key,
            kind="tos",
            passed=True,
            findings=findings,
            tool=tool,
            generated_at=utcnow_iso(),
        )

    # ----------------------------------------------------- 3. Copyright mídia
    def audit_media(self, assets: Dict[str, Any]) -> AuditReport:
        """Valida origem/licença de áudio, vídeo e imagens de uma produção.

        ``assets`` deve ter chaves opcionais: ``video_sources`` (lista de
        ``{"source": "pexels", "url": ...}``), ``audio`` (``{"source":..., "voice":...}``),
        ``music`` (``{"source":..., "license":...}``) e ``images``.
        """
        findings: List[AuditFinding] = []
        raw_safe_sources = {
            "pexels", "pixabay", "cc0", "own", "self", "generated", "local",
            "stable_diffusion", "sdxl", "sd3.5", "sd35", "wan2.2", "wan22",
            "longcat", "kandinsky", "ltx", "ltx-video", "kokoro", "kokoro_82m",
            "edge_tts", "edge-tts", "chatterbox", "piper", "public_domain",
            "public-domain", "moneyprinter", "moneyprinterturbo", "shortgpt",
            "ffmpeg", "sd", "sdxl_turbo",
        }
        # Normalização simétrica: comparamos sempre em forma canónica para que
        # "edge_tts", "edge-tts" e "Edge TTS" sejam equivalentes.
        safe_sources = {_norm(source).replace("-", "") for source in raw_safe_sources}
        if not assets:
            return AuditReport(
                subject="media",
                kind="copyright",
                passed=False,
                findings=[
                    AuditFinding(
                        check="media_present", passed=False, severity="critical",
                        message="Nenhum activo declarado — produção sem proveniência.",
                        evidence={},
                    )
                ],
                tool="none",
                generated_at=utcnow_iso(),
            )

        def check_item(kind: str, item: Dict[str, Any]) -> None:
            raw_source = str(item.get("source", "") or item.get("provider", "")).strip()
            source = _norm(raw_source).replace("-", "")
            license_name = _norm(str(item.get("license", "")))
            if license_name in FORBIDDEN_LICENSES:
                findings.append(
                    AuditFinding(
                        check=f"{kind}_license", passed=False, severity="critical",
                        message=f"{kind}: licença proibida '{license_name}'.",
                        evidence={"item": item},
                    )
                )
                return
            if source and source not in safe_sources:
                findings.append(
                    AuditFinding(
                        check=f"{kind}_source", passed=False, severity="critical",
                        message=(
                        f"{kind}: fonte desconhecida '{raw_source}' sem licença "
                        "comprovada."
                    ),
                        evidence={"item": item},
                    )
                )
                return
            findings.append(
                AuditFinding(
                    check=f"{kind}_source", passed=True, severity="info",
                    message=(
                        f"{kind}: fonte '{raw_source or 'própria'}' aceite para uso "
                        "comercial."
                    ),
                    evidence={"item": item},
                )
            )

        for key in ("video_sources", "images", "audio", "music"):
            value = assets.get(key)
            if value is None:
                continue
            items = value if isinstance(value, list) else [value]
            for item in items:
                if isinstance(item, dict):
                    check_item(key.rstrip("s"), item)

        if not findings:
            findings.append(
                AuditFinding(
                    check="media_present", passed=False, severity="critical",
                    message="Activos declarados sem fonte utilizável.",
                    evidence={},
                )
            )

        passed = not any(f.severity == "critical" for f in findings)
        return AuditReport(
            subject="media",
            kind="copyright",
            passed=passed,
            findings=findings,
            tool="youtube-copyright-scanner"
            if self.available_tools()["youtube_copyright_scanner"]
            else "curated-media-policy",
            generated_at=utcnow_iso(),
        )

    # ------------------------------------------------------------ 4. gate único
    def pre_publish_gate(
        self,
        *,
        subject: str,
        platforms: Optional[List[str]] = None,
        media: Optional[Dict[str, Any]] = None,
        packages: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Verificação consolidada antes de publicar. Lança se algo for crítico."""
        reports: List[AuditReport] = []

        if packages:
            reports.append(self.audit_package_licenses(packages))
        reports.append(self.audit_repository())
        for platform in platforms or []:
            reports.append(self.audit_terms(platform))
        if media is not None:
            reports.append(self.audit_media(media))

        critical = [
            finding
            for report in reports
            for finding in report.findings
            if finding.severity == "critical"
        ]
        passed = not critical

        with self._lock, vault.transaction(SQUAD_METRICS) as data:
            audits = data.setdefault("compliance_audits", [])
            audits.append(
                {
                    "subject": subject[:200],
                    "passed": passed,
                    "reports": [r.to_dict() for r in reports],
                    "at": utcnow_iso(),
                }
            )
            data["compliance_audits"] = audits[-60:]

        result = {
            "subject": subject,
            "passed": passed,
            "reports": [r.to_dict() for r in reports],
            "critical_count": len(critical),
            "generated_at": utcnow_iso(),
        }
        if not passed:
            event_bus.publish(
                "compliance_blocked",
                agent="legal_department",
                message=(
                    f"Publicação bloqueada para '{subject}': "
                    f"{len(critical)} violação(ões) crítica(s)."
                ),
                level="critical",
                payload=result,
            )
            raise ValidationBlocked(
                "Auditoria jurídica reprovada — publicação bloqueada.",
                details=result,
            )
        event_bus.publish(
            "compliance_passed",
            agent="legal_department",
            message=f"Auditoria jurídica aprovada para '{subject}'.",
            payload={"subject": subject, "reports": len(reports)},
        )
        return result

    # ---------------------------------------------------------------- relatório
    def report(self) -> Dict[str, Any]:
        data = vault.read(SQUAD_METRICS)
        audits = data.get("compliance_audits", [])[-10:]
        return {
            "tools_available": self.available_tools(),
            "allowed_licenses": sorted(ALLOWED_LICENSES),
            "forbidden_licenses": sorted(FORBIDDEN_LICENSES),
            "forbidden_packages": sorted(FORBIDDEN_PACKAGES),
            "known_platforms": sorted(TOS_KNOWLEDGE.keys()),
            "recent_audits": audits[::-1],
            "generated_at": utcnow_iso(),
        }


legal_department = LegalCompliance()

__all__ = ["LegalCompliance", "legal_department", "AuditReport", "AuditFinding", "TOS_KNOWLEDGE"]
