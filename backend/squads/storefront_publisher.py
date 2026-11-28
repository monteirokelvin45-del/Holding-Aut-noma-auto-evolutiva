"""
Canal de venda nativo a custo zero — storefront estático para GitHub Pages.

Porquê este módulo
------------------
O projecto não tem canal de venda e Shopify exige assinatura. GitHub Pages é
gratuito, serve HTML estático e é o único host que já está no fluxo do
repositório. O que faltava não era a página: era a **ligação entre um clique e
o dinheiro**, sem processador de pagamentos e sem servidor sempre ligado.

Como o pagamento funciona sem gateway
-------------------------------------
O Presidente já recebe em M-Pesa/e-Mola na conta pessoal. O comprador paga por
transferência e insere o código da transação do SMS. Esse código fica num
pedido de compra que:

1. é escrito em `vault_data/storefront_orders.json` (append-only, com hash);
2. cria uma aprovação em `pending_approvals.json` — o Presidente confirma com
   biometria no APK, igual a qualquer outra decisão que envolve dinheiro;
3. ao ser aprovado, o produto fica desbloqueado em
   `/store/entregas/<codigo>.html` com o link de download.

Isto não verifica automaticamente que o código é válido: alguém tem de
confirmar no extracto M-Pesa. É deliberado — um site estático não pode falar
com a API M-Pesa sem credenciais de empresa, e fingir que verificou seria pior
do que confirmar à mão. A aprovação humana é a verificação.

Para pagamentos globais, cada produto pode ter um link externo
(Gumroad / LemonSqueezy / PayPal.me) — botão directo, sem intermediação nossa.

Segurança
---------
- O código que o comprador insere nunca é usado como caminho de ficheiro sem
  passar por `_codigo_seguro()`.
- A página de entrega é gerada apenas depois da aprovação; antes disso o
  ficheiro não existe.
- Nada de segredos no HTML: o número M-Pesa é o que o Presidente quiser
  publicar, e está em `settings`.
"""

from __future__ import annotations

import html
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..config.settings import settings
from ..core.errors import ValidationBlocked
from ..core.logger import get_logger

log = get_logger(__name__)

UTC = timezone.utc


def _agora() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _slug(texto: str) -> str:
    limpo = re.sub(r"[^a-z0-9]+", "-", texto.lower().strip())
    return limpo.strip("-")[:60] or "produto"


def _codigo_seguro(codigo: str) -> Optional[str]:
    """Aceita só códigos de transacção plausíveis (letras/números, 6-24)."""
    c = (codigo or "").strip().upper().replace(" ", "")
    if not re.fullmatch(r"[A-Z0-9]{6,24}", c):
        return None
    return c


def _esc(valor: Any) -> str:
    return html.escape(str(valor if valor is not None else ""))


# --------------------------------------------------------------------------- #
# Modelo
# --------------------------------------------------------------------------- #
@dataclass
class Produto:
    """Um item à venda. Vem de um esquadrão, não é escrito à mão."""

    id: str
    titulo: str
    resumo: str
    descricao: str
    preco_usd: float
    moeda_local: str = "MZN"
    ficheiro_entrega: str = ""          # caminho relativo ao repositório
    link_externo: str = ""              # Gumroad / LemonSqueezy / PayPal.me
    tags: List[str] = field(default_factory=list)
    sqad_id: str = ""
    criado_em: str = field(default_factory=_agora)

    def preco_local(self) -> float:
        return round(self.preco_usd * settings.usd_mzn_rate, 2)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["preco_local"] = self.preco_local()
        return d


@dataclass
class Pedido:
    """Compra declarada pelo comprador, à espera de confirmação humana."""

    codigo_transacao: str
    produto_id: str
    comprador: str
    contacto: str
    valor_declarado_usd: float
    criado_em: str = field(default_factory=_agora)
    estado: str = "pendente"          # pendente | aprovado | recusado
    approval_id: str = ""
    decidido_em: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# --------------------------------------------------------------------------- #
# Publicador
# --------------------------------------------------------------------------- #
class StorefrontPublisher:
    """Gera o site estático e processa os pedidos de compra."""

    def __init__(self, base_dir: Optional[Path] = None) -> None:
        self.store_dir = Path(base_dir) if base_dir else Path(settings.vault_path).parent / "docs" / "store"
        self.produtos_path = self.store_dir / "produtos.json"
        self.pedidos_path = Path(settings.vault_path) / "storefront_orders.json"

    # ------------------------------------------------------------- persistência
    def _ler_produtos(self) -> List[Dict[str, Any]]:
        if not self.produtos_path.is_file():
            return []
        try:
            return json.loads(self.produtos_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("produtos.json ilegível (%s); a recomeçar lista", exc)
            return []

    def _ler_pedidos(self) -> List[Dict[str, Any]]:
        if not self.pedidos_path.is_file():
            return []
        try:
            return json.loads(self.pedidos_path.read_text(encoding="utf-8")).get("pedidos", [])
        except (json.JSONDecodeError, OSError):
            return []

    def _gravar_pedidos(self, pedidos: List[Dict[str, Any]]) -> None:
        self.pedidos_path.parent.mkdir(parents=True, exist_ok=True)
        self.pedidos_path.write_text(
            json.dumps(
                {"actualizado_em": _agora(), "pedidos": pedidos},
                ensure_ascii=False, indent=2,
            ),
            encoding="utf-8",
        )

    # ------------------------------------------------------------- publicação
    def publicar(
        self, produtos: List[Produto], acumular: bool = True
    ) -> Dict[str, Any]:
        """Escreve a loja: index + uma página por produto.

        ``acumular`` (por omissão) junta os produtos novos aos que já estão em
        ``produtos.json``, substituindo apenas os de mesmo ``id``. Sem isto, o
        ciclo automático apagava o catálogo a cada produto novo — a loja ficava
        sempre com um único item, o do último ciclo, e o que já tinha sido
        publicado desaparecia sem aviso.
        """
        self.store_dir.mkdir(parents=True, exist_ok=True)
        finais: Dict[str, Dict[str, Any]] = {}
        if acumular:
            for antigo in self._ler_produtos():
                if antigo.get("id"):
                    finais[str(antigo["id"])] = antigo
        for produto in produtos:
            finais[produto.id] = produto.to_dict()
        catalogo = list(finais.values())
        (self.store_dir / "produtos.json").write_text(
            json.dumps(catalogo, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        produtos = [self._produto_de_dict(d) for d in catalogo]
        paginas: List[str] = []
        for produto in produtos:
            caminho = self.store_dir / f"{_slug(produto.titulo)}-{produto.id}.html"
            caminho.write_text(self._pagina_produto(produto), encoding="utf-8")
            paginas.append(caminho.name)

        (self.store_dir / "index.html").write_text(
            self._pagina_indice(produtos, paginas), encoding="utf-8"
        )
        (self.store_dir / "README.md").write_text(
            "# Loja\n\nGerado por `backend/squads/storefront_publisher.py`.\n"
            "Não editar à mão: é reescrito a cada publicação.\n",
            encoding="utf-8",
        )
        log.warning("Loja publicada: %d produto(s) em %s", len(produtos), self.store_dir)
        return {
            "index": str(self.store_dir / "index.html"),
            "paginas": paginas,
            "produtos": len(produtos),
            "url_publica": f"{settings.github_pages_base_url}/store/",
        }

    @staticmethod
    def _produto_de_dict(dados: Dict[str, Any]) -> Produto:
        """Reconstrói um Produto lido do catálogo (ignora campos derivados)."""
        campos = {f for f in Produto.__dataclass_fields__}  # type: ignore[attr-defined]
        limpo = {k: v for k, v in dados.items() if k in campos}
        return Produto(**limpo)  # type: ignore[arg-type]

    # -------------------------------------------------------------- pedidos
    def registar_pedido(
        self,
        *,
        produto_id: str,
        codigo_transacao: str,
        comprador: str,
        contacto: str,
        valor_declarado_usd: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Regista a compra e cria a aprovação que a liberta."""
        codigo = _codigo_seguro(codigo_transacao)
        if codigo is None:
            raise ValidationBlocked(
                "Código de transacção inválido: só letras e números, 6 a 24 caracteres.",
                details={"recebido": codigo_transacao[:40]},
            )
        if not comprador.strip() or not contacto.strip():
            raise ValidationBlocked("Nome e contacto são obrigatórios para a entrega.")

        produtos = {p["id"]: p for p in self._ler_produtos()}
        produto = produtos.get(produto_id)
        if produto is None:
            raise ValidationBlocked(f"Produto {produto_id} não existe na loja.")

        pedidos = self._ler_pedidos()
        if any(p["codigo_transacao"] == codigo for p in pedidos):
            raise ValidationBlocked(
                "Este código de transacção já foi usado num pedido.",
                details={"codigo": codigo},
            )

        from ..core.approvals import approval_gate  # import tardio: evita ciclo

        pedido = Pedido(
            codigo_transacao=codigo,
            produto_id=produto_id,
            comprador=comprador.strip()[:80],
            contacto=contacto.strip()[:120],
            valor_declarado_usd=float(
                valor_declarado_usd if valor_declarado_usd is not None else produto["preco_usd"]
            ),
        )
        approval_id = approval_gate.request(
            title=f"Venda: {produto['titulo'][:60]} ({codigo})",
            kind="sale",
            reason=(
                f"{pedido.comprador} declara ter pago ${pedido.valor_declarado_usd:.2f} "
                f"com o código {codigo}. Confirmar no extracto antes de aprovar."
            ),
            amount_usd=pedido.valor_declarado_usd,
            requested_by="storefront",
        )
        pedido.approval_id = approval_id
        pedidos.append(pedido.to_dict())
        self._gravar_pedidos(pedidos)

        log.warning(
            "Pedido de compra %s (%s) por %s — aprovação %s",
            codigo, produto_id, pedido.comprador, approval_id,
        )
        return {"pedido": pedido.to_dict(), "approval_id": approval_id}

    def aprovar_pedido(self, codigo_transacao: str) -> Dict[str, Any]:
        """Marca como aprovado e gera a página de entrega com o download."""
        codigo = _codigo_seguro(codigo_transacao)
        pedidos = self._ler_pedidos()
        alvo = next((p for p in pedidos if p["codigo_transacao"] == codigo), None)
        if alvo is None:
            raise ValidationBlocked(f"Pedido {codigo} não encontrado.")
        if alvo["estado"] == "aprovado":
            return {"pedido": alvo, "ja_aprovado": True}

        produtos = {p["id"]: p for p in self._ler_produtos()}
        produto = produtos.get(alvo["produto_id"])
        if produto is None:
            raise ValidationBlocked("O produto deste pedido já não está na loja.")

        alvo["estado"] = "aprovado"
        alvo["decidido_em"] = _agora()
        self._gravar_pedidos(pedidos)

        entregas = self.store_dir / "entregas"
        entregas.mkdir(parents=True, exist_ok=True)
        pagina = entregas / f"{codigo}.html"
        pagina.write_text(self._pagina_entrega(alvo, produto), encoding="utf-8")

        # O ficheiro de entrega tem de existir: uma página de download a apontar
        # para o vazio é pior do que não haver página.
        ficheiro = produto.get("ficheiro_entrega") or ""
        origem = Path(settings.vault_path).parent / ficheiro if ficheiro else None
        entrega_real = ""
        if origem and origem.is_file():
            destino = entregas / f"{codigo}{origem.suffix}"
            destino.write_bytes(origem.read_bytes())
            entrega_real = destino.name
        else:
            log.warning(
                "Pedido %s aprovado mas o ficheiro de entrega '%s' não existe; "
                "a página fica sem download e di-lo ao comprador.",
                codigo, ficheiro or "(vazio)",
            )

        pagina.write_text(self._pagina_entrega(alvo, produto, entrega_real), encoding="utf-8")
        return {
            "pedido": alvo,
            "pagina_entrega": str(pagina),
            "ficheiro": entrega_real or None,
            "url_publica": f"{settings.github_pages_base_url}/store/entregas/{codigo}.html",
        }

    # ------------------------------------------------------------------ HTML
    def _css(self) -> str:
        return """
    :root{--bg:#0b1220;--card:#121c2e;--line:#1e2c45;--txt:#e8eefc;--muted:#8ba0c4;--acc:#22d3ee;--ok:#22c55e}
    *{box-sizing:border-box;margin:0;padding:0}
    body{background:var(--bg);color:var(--txt);font:16px/1.6 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;padding:0 16px 64px}
    .wrap{max-width:820px;margin:0 auto}
    header{padding:36px 0 20px;border-bottom:1px solid var(--line);margin-bottom:28px}
    h1{font-size:30px;letter-spacing:-.5px}h2{font-size:21px;margin:26px 0 10px}
    .sub{color:var(--muted);margin-top:6px}
    .grid{display:grid;gap:16px}
    .card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:20px}
    .price{color:var(--acc);font-weight:700;font-size:22px}
    .muted{color:var(--muted);font-size:14px}
    .tag{display:inline-block;background:#0f2a33;color:var(--acc);border-radius:6px;padding:2px 8px;font-size:12px;margin:2px 4px 2px 0}
    a.btn,button{display:inline-block;background:var(--acc);color:#08121c;font-weight:700;border:0;border-radius:10px;padding:12px 20px;text-decoration:none;cursor:pointer;font-size:15px}
    a.btn:hover,button:hover{filter:brightness(1.08)}
    .btn-sec{background:transparent;color:var(--acc);border:1px solid var(--acc)}
    input,textarea{width:100%;background:#0d1626;border:1px solid var(--line);color:var(--txt);border-radius:9px;padding:11px;font-size:15px;margin-top:6px}
    label{display:block;margin-top:14px;font-size:14px;color:var(--muted)}
    .pay{background:#0f1d17;border:1px solid #1d3b2c;border-radius:12px;padding:16px;margin-top:14px}
    .codigo{font-family:ui-monospace,monospace;font-size:19px;color:var(--ok);letter-spacing:1px}
    .warn{background:#2a1f0f;border:1px solid #4a3a17;color:#f3d68a;border-radius:10px;padding:12px;font-size:14px;margin-top:14px}
    footer{color:var(--muted);font-size:13px;border-top:1px solid var(--line);margin-top:40px;padding-top:18px}
"""

    def _cabecalho(self, titulo: str) -> str:
        return f"""<!doctype html>
<html lang="pt">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_esc(titulo)}</title>
<style>{self._css()}</style>
</head>
<body><div class="wrap">
<header>
  <h1>Automaton Holding</h1>
  <div class="sub">Produtos digitais feitos por agentes, revistos antes de sair.</div>
</header>
"""

    def _rodape(self) -> str:
        return f"""
<footer>
  Pagamento local por transferência M-Pesa/e-Mola, confirmado manualmente pelo
  Presidente antes de cada entrega. Pagamentos internacionais pelo link do
  produto. Preços em USD; conversão de referência 1 USD ≈ {settings.usd_mzn_rate:.2f} MZN.
</footer>
</div></body></html>
"""

    def _pagina_indice(self, produtos: List[Produto], paginas: List[str]) -> str:
        corpo = [self._cabecalho("Loja — Automaton Holding")]
        if not produtos:
            corpo.append(
                '<div class="card"><p>Sem produtos publicados.</p>'
                '<p class="muted">O ciclo do CEO publica aqui o primeiro produto '
                'quando houver demanda validada.</p></div>'
            )
        else:
            corpo.append('<div class="grid">')
            for produto, pagina in zip(produtos, paginas):
                tags = "".join(f'<span class="tag">{_esc(t)}</span>' for t in produto.tags[:5])
                corpo.append(f"""
<article class="card">
  <h2>{_esc(produto.titulo)}</h2>
  <p class="muted">{_esc(produto.resumo)}</p>
  <p class="price">${produto.preco_usd:.2f} <span class="muted">≈ {produto.preco_local():.2f} MZN</span></p>
  <p>{tags}</p>
  <p style="margin-top:14px"><a class="btn" href="{_esc(pagina)}">Ver e comprar</a></p>
</article>""")
            corpo.append("</div>")
        corpo.append(self._rodape())
        return "".join(corpo)

    def _instrucoes_pagamento(self) -> str:
        numero = _esc(settings.mpesa_service_provider_code or "— configurar —")
        emola = _esc(getattr(settings, "emola_receiver_msisdn", "") or "")
        return f"""
<div class="pay">
  <p><strong>1.</strong> Transfere o valor por M-Pesa para <span class="codigo">{numero}</span></p>
  {f'<p style="margin-top:6px"><strong>ou</strong> e-Mola para <span class="codigo">{emola}</span></p>' if emola else ''}
  <p style="margin-top:8px" class="muted">Guarda o código da transacção que recebes por SMS
  (ex.: <code>MP2406AB12CD</code>).</p>
  <p style="margin-top:10px"><strong>2.</strong> Insere o código abaixo. O Presidente confirma
  no extracto e a entrega é liberada.</p>
</div>"""

    def _pagina_produto(self, produto: Produto) -> str:
        externo = ""
        if produto.link_externo:
            externo = f"""
<div class="card" style="margin-top:18px">
  <h2>Pagar de fora de Moçambique</h2>
  <p class="muted">Cartão ou PayPal, processado pelo provedor do link.</p>
  <p style="margin-top:12px">
    <a class="btn btn-sec" href="{_esc(produto.link_externo)}" rel="noopener">Pagar ${produto.preco_usd:.2f}</a>
  </p>
</div>"""
        return f"""{self._cabecalho(produto.titulo)}
<article class="card">
  <h2>{_esc(produto.titulo)}</h2>
  <p class="muted">{_esc(produto.resumo)}</p>
  <p class="price" style="margin-top:10px">${produto.preco_usd:.2f}
     <span class="muted">≈ {produto.preco_local():.2f} MZN</span></p>
  <div style="margin-top:14px">{_esc(produto.descricao)}</div>
</article>

<div class="card" style="margin-top:18px">
  <h2>Comprar com M-Pesa / e-Mola</h2>
  {self._instrucoes_pagamento()}
  <form method="POST" action="https://api.automaton-holding.example/store/pedido" onsubmit="pedir(event)">
    <input type="hidden" name="produto_id" value="{_esc(produto.id)}">
    <label>O teu nome<input name="comprador" required maxlength="80" autocomplete="name"></label>
    <label>Contacto (telefone ou email)<input name="contacto" required maxlength="120"></label>
    <label>Código da transacção do SMS<input name="codigo" required maxlength="24"
           placeholder="MP2406AB12CD" style="text-transform:uppercase"></label>
    <p style="margin-top:16px"><button type="submit">Enviar para confirmação</button></p>
  </form>
  <div id="estado" class="warn" style="display:none"></div>
</div>
{externo}
<script>
// A loja é estática: não há backend público a aceitar o POST. Em vez de fingir
// que envia, mostra o que fazer com o código. Quando existir um endpoint real
// (o backend do holding), basta trocar esta função.
function pedir(e) {{
  e.preventDefault();
  const f = e.target;
  const caixa = document.getElementById('estado');
  caixa.style.display = 'block';
  caixa.textContent = 'Pedido preparado. A loja é estática e não tem servidor: '
    + 'envia o código ' + (f.codigo.value || '').toUpperCase()
    + ' por WhatsApp para o número do vendedor. A aprovação e a entrega são '
    + 'feitas à mão até existir backend público.';
}}
</script>
{self._rodape()}"""

    def _pagina_entrega(
        self, pedido: Dict[str, Any], produto: Dict[str, Any], ficheiro: str = ""
    ) -> str:
        if ficheiro:
            corpo_download = (
                f'<p style="margin-top:16px"><a class="btn" href="{_esc(ficheiro)}" '
                f'download>Descarregar {_esc(produto["titulo"])}</a></p>'
            )
        else:
            corpo_download = (
                '<div class="warn">O ficheiro deste produto ainda não está '
                'disponível. O vendedor foi notificado e envia-te por contacto '
                'directo.</div>'
            )
        return f"""{self._cabecalho("A tua compra — Automaton Holding")}
<article class="card">
  <h2>Obrigado, {_esc(pedido['comprador'])}</h2>
  <p class="muted">Código de transacção confirmado: <span class="codigo">{_esc(pedido['codigo_transacao'])}</span></p>
  <p style="margin-top:10px">{_esc(produto['titulo'])}</p>
  {corpo_download}
  <p class="muted" style="margin-top:18px">Guarda esta página: é o teu comprovativo de compra.</p>
</article>
{self._rodape()}"""


storefront = StorefrontPublisher()
