"""Registo real das ferramentas que os agentes declaram nos YAML.

Porque este ficheiro existe
---------------------------
``config/agents.yaml`` declara, por agente, uma lista de nomes de ferramentas:
``crawl4ai``, ``ffmpeg``, ``eval_judge``, ``approval_gate``... Durante muito
tempo esses nomes não eram resolvidos para nada. O ``crew_bridge`` lia-os como
metadado e seguia em frente, o que dava uma situação pior do que não ter
ferramentas: um agente a quem se diz "tens grep e compliance" e que não tem.

Ao mapear os nomes um a um apareceu a razão de ninguém ter dado por isso:
**quase todas as ferramentas já existiam, com outro nome.** ``ffmpeg`` é
``motion_renderer.ffmpeg``; ``scancode`` é
``legal_department.scan_path_with_scancode``; ``rubric_scorer`` é
``eval_judge.evaluate``. Não faltava código — faltava a ligação.

Três estados, e a diferença importa
-----------------------------------
* ``pronta``      — objecto existe e as dependências estão presentes.
* ``degradada``   — objecto existe, mas faz falta um pacote/chave e a
                    ferramenta cai para uma alternativa mais fraca. Dizer
                    "degradada" é diferente de dizer "pronta": muda o que o
                    agente pode prometer ao utilizador.
* ``em_falta``    — não existe implementação. Hoje são duas.

O que este ficheiro **não** faz
-------------------------------
Não inventa métodos de conveniência para tapar buracos. Quando uma fonte
não tem implementação, fica registada como não implementada e a razão fica
escrita. Um ``noop`` silencioso é pior do que uma lacuna conhecida: o
agente acredita que produziu algo.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from .logger import get_logger

log = get_logger("core.tool_registry")


# ==========================================================================
# Especificação
# ==========================================================================
@dataclass(frozen=True)
class ToolEntry:
    """Uma ferramenta declarada num YAML, ligada (ou não) a código real."""

    nome: str
    modulo: str = ""
    objecto: str = ""
    metodo: str = ""
    descricao: str = ""
    #: Pacotes/chaves sem os quais a ferramenta funciona em modo reduzido.
    requer: tuple = ()
    #: Para onde cai quando falta o que está em `requer`.
    alternativa: str = ""
    #: Razão, quando não há implementação nenhuma.
    motivo_em_falta: str = ""
    #: Métodos privados são internos do squad; chamá-los é permitido mas
    #: quem o faz deve saber que não há contrato público.
    interno: bool = False

    # ---------------------------------------------------------------- estado
    def _importar(self) -> Any:
        return importlib.import_module(self.modulo)

    def objecto_real(self) -> Any:
        """O objecto resolvido, ou ``None`` se não existir."""
        if not self.modulo:
            return None
        try:
            modulo = self._importar()
        except Exception as exc:  # noqa: BLE001
            log.debug("Tool %s: módulo %s indisponível (%s)", self.nome, self.modulo, exc)
            return None
        if not self.objecto:
            return modulo
        return getattr(modulo, self.objecto, None)

    def funcao(self) -> Optional[Callable[..., Any]]:
        """A função chamável, ou ``None``.

        Um ``objecto`` sem ``metodo`` devolve-se a si mesmo se for chamável
        (casos como ``kill_switch``, que é um objecto com métodos).
        """
        objecto = self.objecto_real()
        if objecto is None:
            return None
        if self.metodo:
            funcao = getattr(objecto, self.metodo, None)
            return funcao if callable(funcao) else None
        return objecto if callable(objecto) else None

    def disponivel(self) -> bool:
        return self.funcao() is not None

    def faltam(self) -> List[str]:
        """Dependências de ``requer`` que não estão presentes."""
        faltam: List[str] = []
        for requisito in self.requer:
            if not _satisfeito(requisito):
                faltam.append(requisito)
        return faltam

    @property
    def estado(self) -> str:
        if not self.disponivel():
            return "em_falta"
        return "degradada" if self.faltam() else "pronta"

    def to_dict(self) -> Dict[str, Any]:
        dados: Dict[str, Any] = {
            "nome": self.nome,
            "estado": self.estado,
            "descricao": self.descricao,
            "implementacao": (
                f"{self.modulo}.{self.objecto}" + (f".{self.metodo}" if self.metodo else "")
                if self.modulo
                else ""
            ),
        }
        if self.requer:
            dados["requer"] = list(self.requer)
            dados["faltam"] = self.faltam()
        if self.alternativa:
            dados["alternativa"] = self.alternativa
        if self.motivo_em_falta:
            dados["motivo"] = self.motivo_em_falta
        if self.interno:
            dados["interno"] = True
        return dados


def _satisfeito(requisito: str) -> bool:
    """Um requisito é um pacote importável, ou ``chave:NOME`` num setting."""
    if requisito.startswith("chave:"):
        from ..config.settings import settings

        atributo = requisito.split(":", 1)[1]
        return bool(getattr(settings, atributo, None))
    if requisito.startswith("bin:"):
        import shutil

        return shutil.which(requisito.split(":", 1)[1]) is not None
    try:
        importlib.import_module(requisito)
    except Exception:  # noqa: BLE001
        return False
    return True


# ==========================================================================
# O mapa — nome declarado no YAML -> código que existe
# ==========================================================================
_K = (
    # ----------------------------------------------------------- descoberta
    ToolEntry(
        "crawl4ai", "backend.squads.research_backends", "research_backends", "availability",
        "Lê páginas reais para pesquisa; sem o pacote cai no extractor nativo.",
        requer=("crawl4ai",), alternativa="extractor nativo (urllib + leitura de HTML)",
    ),
    ToolEntry(
        "gpt_researcher", "backend.squads.research_backends", "research_backends", "deep_research",
        "Pesquisa profunda multi-fonte; sem o pacote usa o nativo.",
        requer=("gpt_researcher",), alternativa="research_backends nativo",
    ),
    ToolEntry(
        "github_trending", "backend.squads.scout_hunter", "scout_hunter", "_fetch_github_trending",
        "Tendências do GitHub como sinal de demanda.", interno=True,
    ),
    ToolEntry(
        "github_api", "backend.squads.scout_hunter", "scout_hunter", "_fetch_github_trending",
        "API pública do GitHub (sem chave para leitura).", interno=True,
    ),
    ToolEntry(
        "reddit_api", "backend.squads.scout_hunter", "scout_hunter", "_fetch_reddit",
        "Subreddits como sinal de dor/demanda.", interno=True,
    ),
    # ----------------------------------------------------------- validação
    ToolEntry(
        "landing_page_generator", "backend.validation.demand_validator", "demand_validator", "render_page",
        "Gera a landing page de teste de demanda (HTML local).",
    ),
    ToolEntry(
        "analytics_tracker", "backend.validation.demand_validator", "demand_validator", "record_event",
        "Conta visitas/cliques/compradores do teste de demanda.",
    ),
    ToolEntry(
        "rubric_scorer", "backend.validation.eval_judge", "eval_judge", "evaluate",
        "Pontua um artefacto contra a rubrica antes de publicar.",
    ),
    ToolEntry(
        "objective_checks", "backend.validation.eval_judge", "eval_judge", "enforce",
        "Verificações objectivas (tamanho, links, campos) e bloqueio.",
    ),
    # ----------------------------------------------------------- vídeo
    ToolEntry(
        "ffmpeg", "backend.squads.motion_renderer", "motion_renderer", "ffmpeg",
        "Codificação/edição de vídeo local (binário empacotado, offline).",
    ),
    ToolEntry(
        "edge_tts", "backend.squads.kokoro_tts", "kokoro_local", "sintetizar",
        "Narração por voz local (Kokoro/edge); offline quando o modelo existe.",
        requer=("kokoro",), alternativa="vozes locais de sistema / sem narração",
    ),
    ToolEntry(
        "kokoro_tts", "backend.squads.kokoro_tts", "kokoro_local", "sintetizar",
        "Voz neural local, sem internet e sem chave.",
        requer=("kokoro",), alternativa="edge-tts ou sem narração",
    ),
    ToolEntry(
        "moneyprinterturbo", "backend.squads.video_factory_squad", "video_factory", "produzir_offline",
        "Pipeline de vídeo curto 100% offline (substitui o MoneyPrinter Turbo).",
    ),
    ToolEntry(
        "pexels", "backend.squads.video_factory_squad", "video_factory", "render",
        "Banco de imagens; exige chave e internet — desligado por omissão.",
        requer=("chave:pexels_api_key",), alternativa="footage local/gerado",
    ),
    ToolEntry(
        "pixabay", "backend.squads.video_factory_squad", "video_factory", "render",
        "Banco de imagens; exige chave e internet — desligado por omissão.",
        requer=("chave:pixabay_api_key",), alternativa="footage local/gerado",
    ),
    # ----------------------------------------------------------- produto
    ToolEntry(
        "seo_writer", "backend.squads.infoproducts_affiliate_squad", "infoproducts_squad", "render_article_html",
        "Escrita/render de artigo optimizado (HTML local).",
    ),
    ToolEntry(
        "gumroad_client", "backend.squads.storefront_publisher", "storefront", "publicar",
        "Loja própria local; Gumroad real exigiria chave.",
        requer=("chave:gumroad_token",), alternativa="storefront local (checkout manual)",
    ),
    ToolEntry(
        "lemonsqueezy_client", "backend.squads.storefront_publisher", "storefront", "publicar",
        "Loja própria local; Lemon Squeezy real exigiria chave.",
        requer=("chave:lemonsqueezy_api_key",), alternativa="storefront local",
    ),
    ToolEntry(
        "affiliate_builder", "backend.squads.infoproducts_affiliate_squad", "infoproducts_squad", "write_ebook",
        "Constrói o produto (e-book/guia) e o material de afiliação.",
    ),
    # ----------------------------------------------------------- engenharia
    ToolEntry(
        "code_generator", "backend.squads.micro_saas_dev_squad", "micro_saas_squad", "generate_code",
        "Gera o código do micro-SaaS a partir do blueprint.",
    ),
    ToolEntry(
        "qa_runner", "backend.squads.micro_saas_dev_squad", "micro_saas_squad", "quality_assurance",
        "Corre os testes do micro-SaaS gerado antes de publicar.",
    ),
    ToolEntry(
        "metagpt_pipeline", "",
        descricao="Pipeline MetaGPT (multi-agente) para gerar apps.",
        motivo_em_falta=(
            "MetaGPT não está instalado e não é MIT no pacote completo; o "
            "micro_saas_squad gera o mesmo artefacto sem ele. Registado como "
            "lacuna consciente, não como falha silenciosa."
        ),
    ),
    ToolEntry(
        "isolated_container", "backend.squads.micro_saas_dev_squad", "micro_saas_squad", "quality_assurance",
        descricao=(
            "Execução dos testes gerados. Isolamento por container exige o "
            "serviço 'micro_saas' do docker-compose; sem ele os testes correm "
            "no processo actual."
        ),
        requer=("docker",), alternativa="execução no processo (menos isolada)",
    ),
    # ----------------------------------------------------------- governação
    ToolEntry(
        "scancode", "backend.governance.legal_compliance", "legal_department", "scan_path_with_scancode",
        "Auditoria de licenças por ficheiro (ScanCode).",
        requer=("scancode_toolkit",), alternativa="auditoria por extensão/pacote (audit_package_licenses)",
    ),
    ToolEntry(
        "licscan", "backend.governance.legal_compliance", "legal_department", "audit_package_licenses",
        descricao=(
            "Lista de licenças por pacote instalado. Nota: o pacote `liscscan` "
            "NÃO existe no PyPI — a auditoria é feita por legal_compliance."
        ),
    ),
    ToolEntry(
        "tos_rag_analyzer", "backend.governance.legal_compliance", "legal_department", "audit_terms",
        "Lê termos de serviço e assinala cláusulas de risco.",
    ),
    ToolEntry(
        "youtube_copyright_scanner", "backend.governance.legal_compliance", "legal_department", "audit_media",
        "Verifica música/vídeo/imagens contra direitos de autor antes de publicar.",
    ),
    ToolEntry(
        "legal_compliance", "backend.governance.legal_compliance", "legal_department", "pre_publish_gate",
        "Portão de publicação: licença, direitos, conformidade.",
    ),
    ToolEntry(
        "cycle_runner", "backend.governance.ceo_orchestrator", "ceo", "run_cycle",
        "Corre um ciclo completo da holding (CEO).",
    ),
    ToolEntry(
        "approval_gate", "backend.core.approvals", "approval_gate", "request",
        "Cria pedidos pendentes de aprovação do Presidente.",
    ),
    ToolEntry(
        "kill_switch", "backend.core.security_guard", "kill_switch", "status",
        "Estado de paragem de emergência de cada subsistema.",
    ),
    ToolEntry(
        "prompt_optimizer", "backend.governance.hr_department", "hr_department", "review_agent",
        "Revê o prompt de um agente a partir do desempenho medido.",
    ),
    ToolEntry(
        "github_search", "backend.governance.hr_department", "hr_department", "agents",
        "Catálogo de agentes como base de contratação/revisão (RH).",
    ),
    ToolEntry(
        "performance_metrics", "backend.governance.hr_department", "hr_department", "report",
        "Relatório de desempenho por agente.",
    ),
    ToolEntry(
        "metrics_engine", "backend.governance.accountant_auditor", "accountant", "financial_report",
        "Relatório financeiro/P&L por squad.",
    ),
    ToolEntry(
        "post_mortem_writer", "backend.governance.accountant_auditor", "accountant", "evaluate_squad",
        "Veredicto por squad: continuar, pausar ou matar.",
    ),
    # ----------------------------------------------------------- infra
    ToolEntry(
        "llm_router", "backend.core.llm_router", "llm_router", "complete",
        "Roteador de LLM com orçamento $0.00 e fallback local.",
    ),
    ToolEntry(
        "grounding_guard", "backend.core.grounding", "", "problemas",
        "Verifica factos de Moçambique e remove afirmações sem âncora.",
    ),
    ToolEntry(
        "shared_memory", "backend.core.shared_memory", "shared_memory", "list_entries",
        descricao="Cemitério de tentativas falhadas: impede repetir o mesmo erro.",
    ),
    ToolEntry(
        "vault_store", "backend.core.vault_store", "vault", "read",
        "Cofre de ficheiros com escrita atómica e bloqueio.",
    ),
    ToolEntry(
        "event_bus", "backend.core.runtime_bus", "event_bus", "publish",
        "Barramento de eventos em tempo real (websocket).",
    ),
    ToolEntry(
        "telegram_bot", "backend.core.observability_langfuse", "telegram_client", "send",
        "Bot do Telegram para aprovações e avisos.",
        requer=("chave:telegram_bot_token",), alternativa="notificações na app/observabilidade",
    ),
    # ----------------------------------------------------------- dinheiro
    ToolEntry(
        "ledger_tools", "backend.finance.cfo_treasury", "cfo", "snapshot",
        "Livro-razão e saldos do cofre.",
    ),
    ToolEntry(
        "payout_bridge", "backend.finance.payout_bridge", "payout_bridge", "request_payout",
        "Executa saques por M-Pesa, e-Mola, PayPal ou Wise (exige biometria).",
    ),
    ToolEntry(
        "paypal_cobranca", "backend.finance.paypal_cobranca", "paypal_cobranca", "criar_cobranca",
        "Cria e captura cobranças PayPal (o lado que RECEBE).",
        requer=("chave:paypal_client_id",), alternativa="dry-run explícito (não inventa pedidos)",
    ),
    ToolEntry(
        "fx_rates", "backend.finance.cfo_treasury", "cfo", "snapshot",
        "Câmbio USD/MZN usado na tesouraria.",
    ),
    ToolEntry(
        "receber_pagamento", "backend.finance.cfo_tools", "receber_pagamento",
        descricao="Regista entrada de dinheiro e aplica 5% + divisão por fase.",
    ),
    ToolEntry(
        "dividir_lucro", "backend.finance.cfo_tools", "dividir_lucro",
        descricao="Simula a divisão da fase; não regista nada.",
    ),
    ToolEntry(
        "registrar_saques", "backend.finance.cfo_tools", "registrar_saques",
        descricao="Cria pedido de saque pendente de biometria; nunca envia dinheiro.",
    ),
)

#: nome declarado -> entrada
TOOLS: Dict[str, ToolEntry] = {entrada.nome: entrada for entrada in _K}


# ==========================================================================
# API
# ==========================================================================
def obter(nome: str) -> Optional[ToolEntry]:
    return TOOLS.get(nome)


def funcao(nome: str) -> Optional[Callable[..., Any]]:
    entrada = TOOLS.get(nome)
    return entrada.funcao() if entrada else None


def invocar(nome: str, /, **kwargs: Any) -> Any:
    """Chama a ferramenta. Levanta erro claro se não estiver disponível.

    Não há fallback silencioso: se a ferramenta não existe, quem chamou fica
    a saber. Um agente que recebe ``None`` e continua a inventar resultados é
    o pior desfecho possível.
    """
    entrada = TOOLS.get(nome)
    if entrada is None:
        raise KeyError(f"Ferramenta não registada: {nome!r}")
    alvo = entrada.funcao()
    if alvo is None:
        raise RuntimeError(
            f"Ferramenta {nome!r} sem implementação: {entrada.motivo_em_falta or 'sem módulo'}"
        )
    return alvo(**kwargs)


def estado() -> Dict[str, Any]:
    """Contagem por estado, e todas as entradas em ``em_falta``."""
    prontas = [n for n, e in TOOLS.items() if e.estado == "pronta"]
    degradadas = [n for n, e in TOOLS.items() if e.estado == "degradada"]
    em_falta = [n for n, e in TOOLS.items() if e.estado == "em_falta"]
    return {
        "total": len(TOOLS),
        "prontas": len(prontas),
        "degradadas": len(degradadas),
        "em_falta": len(em_falta),
        "lista_prontas": sorted(prontas),
        "lista_degradadas": sorted(degradadas),
        "lista_em_falta": sorted(em_falta),
        "detalhe_degradadas": {
            n: TOOLS[n].to_dict() for n in sorted(degradadas)
        },
        "detalhe_em_falta": {n: TOOLS[n].to_dict() for n in sorted(em_falta)},
    }


def cobertura(por_agente: Dict[str, List[str]]) -> Dict[str, Any]:
    """Cruza o que cada agente declara com o estado real de cada ferramenta."""
    desconhecidas: Dict[str, List[str]] = {}
    resumo: Dict[str, Dict[str, Any]] = {}
    for agente, declaradas in por_agente.items():
        estados = {"pronta": 0, "degradada": 0, "em_falta": 0}
        faltam: List[str] = []
        for nome in declaradas:
            entrada = TOOLS.get(nome)
            if entrada is None:
                desconhecidas.setdefault(agente, []).append(nome)
                continue
            estados[entrada.estado] += 1
            if entrada.estado != "pronta":
                faltam.append(nome)
        resumo[agente] = {"contagem": estados, "nao_prontas": faltam}
    return {
        "por_agente": resumo,
        "desconhecidas": desconhecidas,
        "estado_global": estado(),
        "nota": (
            "`degradada` significa que a ferramenta funciona em modo reduzido "
            "(falta um pacote ou uma chave). `em_falta` significa que não há "
            "implementação. São coisas diferentes e ficam separadas."
        ),
    }
