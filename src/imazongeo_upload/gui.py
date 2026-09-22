"""Interface gráfica (Tkinter) para upload dos datasets Imazon para o S3.

Datasets: SAD / Floreser / Ameaça & Pressão / SIMEX.

Uso::

    imazongeo-upload-gui          # ou: python -m imazongeo_upload.gui

No Ubuntu, o Tkinter vem do pacote do sistema ``python3-tk``. Para gerar
um executável Windows (.exe), veja ``scripts/windows/build_exe.py``.
"""

from __future__ import annotations

import logging
import os
import queue
import sys
import threading
import tkinter as tk
from datetime import date
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, simpledialog, ttk

from dotenv import load_dotenv

from .ameaca_pressao import processar_ameaca_pressao_dashboard
from .config import LOG_FORMAT, aws_region, load_env
from .datasets import (
    DATASETS,
    S3_DASHBOARD_BASE,
    descobrir_periodo,
    montar_nome_arquivo,
    prefixo_dashboard,
)
from .processing import processar_dataset
from .s3 import create_s3_client
from .sad import inspecionar_zips_sad, processar_sad_zip
from .simex import processar_simex_dashboard
from .simulador import executar_simulacao

# Fontes disponíveis por padrão em cada sistema (no Ubuntu, DejaVu)
if sys.platform == "win32":
    _FONTE, _FONTE_MONO = "Segoe UI", "Consolas"
elif sys.platform == "darwin":
    _FONTE, _FONTE_MONO = "Helvetica", "Menlo"
else:
    _FONTE, _FONTE_MONO = "DejaVu Sans", "DejaVu Sans Mono"


def _carregar_env() -> None:
    """Carrega o .env (no .exe, também o empacotado pelo PyInstaller)."""
    if getattr(sys, "frozen", False):
        # Rodando como .exe (PyInstaller --onefile): as credenciais viajam
        # empacotadas dentro do próprio executável (veja build_exe.py), para
        # que o programa funcione em qualquer computador sem exigir um .env
        # externo.
        #
        # Primeiro tenta um .env ao lado do .exe (permite sobrescrever as
        # credenciais embutidas sem precisar gerar um novo executável) e, se
        # alguma variável não for encontrada, cai para o .env empacotado.
        load_dotenv(Path(sys.executable).parent / ".env")
        load_dotenv(Path(getattr(sys, "_MEIPASS", "")) / ".env")
    else:
        load_env()


# ---------------------------------------------------------------------------
# Logging handler que alimenta uma fila para a GUI consumir
# ---------------------------------------------------------------------------


class _QueueHandler(logging.Handler):
    def __init__(self, log_queue: queue.Queue[str]) -> None:
        super().__init__()
        self.log_queue = log_queue

    def emit(self, record: logging.LogRecord) -> None:
        self.log_queue.put(self.format(record))


# ---------------------------------------------------------------------------
# Aplicação principal
# ---------------------------------------------------------------------------


class ImazonUploadApp(tk.Tk):
    # Paleta de cores
    _BG = "#1a1f2e"
    _BG2 = "#242938"
    _BG3 = "#2d3347"
    _ACCENT = "#4c9be8"
    _FG = "#dce3f0"
    _FG_DIM = "#8a94ad"
    _GREEN = "#3fb950"
    _YELLOW = "#d29922"
    _RED = "#f85149"
    _BLUE = "#58a6ff"
    _LOG_BG = "#0d1117"

    # Espaçamento padronizado (todas as seções usam os mesmos valores)
    _LF_PADDING = (10, 8)  # padding interno dos LabelFrame
    _SECTION_GAP = (0, 12)  # espaço vertical entre seções
    _ITEM_PADY = (
        3  # espaço vertical entre itens (radio/checkbutton) dentro de uma seção
    )

    def __init__(self) -> None:
        super().__init__()
        self.title("Imazon · Upload S3")
        self.geometry("900x680")
        self.minsize(800, 580)
        self.configure(bg=self._BG)
        self._apply_styles()
        self._build_ui()
        self._setup_logging()
        self._on_operacao_change()  # atualiza formatos disponíveis + guia inicial
        self._on_camada_tab_change()  # SAD (aba inicial) não usa "Operação"

    # ------------------------------------------------------------------
    # Estilos ttk
    # ------------------------------------------------------------------

    def _apply_styles(self) -> None:
        s = ttk.Style(self)
        s.theme_use("clam")

        s.configure(".", background=self._BG, foreground=self._FG, font=(_FONTE, 9))

        for w in ("TFrame", "TLabelframe"):
            s.configure(w, background=self._BG)

        s.configure(
            "TLabelframe.Label",
            background=self._BG,
            foreground=self._ACCENT,
            font=(_FONTE, 9, "bold"),
        )

        s.configure("TLabel", background=self._BG, foreground=self._FG)
        s.configure("Dim.TLabel", background=self._BG, foreground=self._FG_DIM)
        s.configure(
            "Header.TLabel",
            background=self._BG,
            foreground=self._ACCENT,
            font=(_FONTE, 17, "bold"),
        )
        s.configure(
            "Sub.TLabel", background=self._BG, foreground=self._FG_DIM, font=(_FONTE, 9)
        )

        s.configure("TRadiobutton", background=self._BG, foreground=self._FG)
        s.map("TRadiobutton", background=[("active", self._BG)])

        s.configure("TCheckbutton", background=self._BG, foreground=self._FG)
        s.map("TCheckbutton", background=[("active", self._BG)])

        s.configure(
            "TEntry",
            fieldbackground=self._BG3,
            foreground=self._FG,
            insertcolor=self._FG,
            bordercolor=self._BG3,
            relief="flat",
        )
        # Campo "readonly" (travado, não editável) fica visualmente mais
        # escuro/apagado do que um campo editável normal, deixando claro
        # que não dá pra digitar ali.
        s.map(
            "TEntry",
            fieldbackground=[("readonly", self._BG2)],
            foreground=[("readonly", self._FG_DIM)],
        )

        s.configure(
            "TSpinbox",
            fieldbackground=self._BG3,
            foreground=self._FG,
            insertcolor=self._FG,
            bordercolor=self._BG3,
            relief="flat",
            arrowcolor=self._FG_DIM,
        )

        s.configure(
            "TCombobox",
            fieldbackground=self._BG3,
            foreground=self._FG,
            selectbackground=self._BG3,
            selectforeground=self._FG,
        )
        s.map(
            "TCombobox",
            fieldbackground=[("readonly", self._BG3)],
            foreground=[("readonly", self._FG)],
        )

        s.configure(
            "TButton",
            background=self._BG3,
            foreground=self._FG,
            relief="flat",
            padding=(8, 4),
        )
        s.map("TButton", background=[("active", "#3a4158"), ("disabled", self._BG2)])

        s.configure(
            "Run.TButton",
            background="#1a6b2e",
            foreground="white",
            font=(_FONTE, 10, "bold"),
            padding=(14, 6),
        )
        s.map(
            "Run.TButton", background=[("active", "#145924"), ("disabled", self._BG3)]
        )

        s.configure("TSeparator", background=self._BG3)

        s.configure(
            "TNotebook", background=self._BG, borderwidth=0, tabmargins=(0, 4, 0, 0)
        )
        s.configure(
            "TNotebook.Tab",
            background=self._BG2,
            foreground=self._FG_DIM,
            padding=(14, 7),
            font=(_FONTE, 9, "bold"),
            borderwidth=0,
        )
        s.map(
            "TNotebook.Tab",
            background=[("selected", self._BG3)],
            foreground=[("selected", self._ACCENT)],
        )

    # ------------------------------------------------------------------
    # Construção da UI
    # ------------------------------------------------------------------

    def _make_scrollable(self, tab: ttk.Frame) -> ttk.Frame:
        """Envolve o conteúdo de uma aba num Canvas rolável.

        Necessário porque o conteúdo de algumas abas (ex.: "Upload", com
        4 seções empilhadas) pode não caber na altura disponível da
        janela — sem isso, o final da aba fica cortado e inacessível.
        Retorna o frame onde o conteúdo da aba deve ser construído.
        """
        tab.rowconfigure(0, weight=1)
        tab.columnconfigure(0, weight=1)

        canvas = tk.Canvas(tab, bg=self._BG, highlightthickness=0)
        canvas.grid(row=0, column=0, sticky="nsew")

        scrollbar = ttk.Scrollbar(tab, orient="vertical", command=canvas.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        canvas.configure(yscrollcommand=scrollbar.set)

        content = ttk.Frame(canvas)
        content_window = canvas.create_window((0, 0), window=content, anchor="nw")

        def _on_content_configure(_event):
            canvas.configure(scrollregion=canvas.bbox("all"))

        def _on_canvas_configure(event):
            canvas.itemconfig(content_window, width=event.width)

        content.bind("<Configure>", _on_content_configure)
        canvas.bind("<Configure>", _on_canvas_configure)

        # Scroll com roda do mouse, ativo só enquanto o cursor está sobre
        # esta aba (não "vaza" para outras abas/widgets da janela). No
        # Windows/macOS a roda gera <MouseWheel>; no Linux (X11), os botões
        # 4 (para cima) e 5 (para baixo).
        def _on_mousewheel(event):
            canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

        def _on_button_scroll(event):
            canvas.yview_scroll(-1 if event.num == 4 else 1, "units")

        def _bind_mousewheel(_event):
            canvas.bind_all("<MouseWheel>", _on_mousewheel)
            canvas.bind_all("<Button-4>", _on_button_scroll)
            canvas.bind_all("<Button-5>", _on_button_scroll)

        def _unbind_mousewheel(_event):
            for sequencia in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
                canvas.unbind_all(sequencia)

        canvas.bind("<Enter>", _bind_mousewheel)
        canvas.bind("<Leave>", _unbind_mousewheel)

        return content

    def _build_ui(self) -> None:
        self._build_header()
        ttk.Separator(self, orient="horizontal").pack(fill=tk.X, padx=0)

        # Barra inferior (deve ser empacotada antes do main para não ser cortada)
        self._build_footer()

        # Área principal: coluna esquerda (configuração, em abas) + direita
        # (guia/log, em abas)
        main = ttk.Frame(self, padding=(16, 12, 16, 0))
        main.pack(fill=tk.BOTH, expand=True)
        main.columnconfigure(0, weight=0, minsize=340)
        main.columnconfigure(1, weight=1)
        main.rowconfigure(0, weight=1)

        # ---- Coluna esquerda: abas de configuração --------------------
        left_nb = ttk.Notebook(main)
        left_nb.grid(row=0, column=0, sticky="nsew", padx=(0, 12))

        tab_upload = ttk.Frame(left_nb)
        tab_aws = ttk.Frame(left_nb)
        tab_opcoes = ttk.Frame(left_nb)

        left_nb.add(tab_upload, text="Upload")
        left_nb.add(tab_aws, text="Configuração AWS")
        left_nb.add(tab_opcoes, text="Opções")

        # Cada aba é rolável: o conteúdo pode ser mais alto do que a janela
        # permite mostrar de uma vez (ex.: "Upload" tem 4 seções empilhadas).
        content_upload = self._make_scrollable(tab_upload)
        content_upload.configure(padding=(12, 10))
        content_aws = self._make_scrollable(tab_aws)
        content_aws.configure(padding=(12, 10))
        content_opcoes = self._make_scrollable(tab_opcoes)
        content_opcoes.configure(padding=(12, 10))

        self._build_operacao_section(content_upload)
        self._build_camadas_section(content_upload)

        self._build_aws_section(content_aws)

        self._build_options_section(content_opcoes)

        # ---- Coluna direita: abas de guia e log ------------------------
        right_nb = ttk.Notebook(main)
        right_nb.grid(row=0, column=1, sticky="nsew")

        tab_guia = ttk.Frame(right_nb, padding=(12, 10))
        tab_log = ttk.Frame(right_nb, padding=(10, 8))

        right_nb.add(tab_guia, text="Guia de uso")
        right_nb.add(tab_log, text="Log de execução")

        self._build_tutorial_section(tab_guia)
        self._build_log_section(tab_log)

    def _build_header(self) -> None:
        f = ttk.Frame(self, padding=(20, 14, 20, 12))
        f.pack(fill=tk.X)
        ttk.Label(f, text="Imazon", style="Header.TLabel").pack(side=tk.LEFT)
        ttk.Label(f, text="Upload de datasets para AWS S3", style="Sub.TLabel").pack(
            side=tk.LEFT, padx=(10, 0), anchor="s", pady=(0, 2)
        )

    def _build_operacao_section(self, parent: ttk.Frame) -> None:
        frame = ttk.LabelFrame(parent, text="Operação", padding=self._LF_PADDING)
        frame.pack(fill=tk.X, pady=self._SECTION_GAP)

        self.op_var = tk.StringVar(value="download")
        self._op_radios = []

        rb = ttk.Radiobutton(
            frame,
            text="Atualizar arquivos de download",
            variable=self.op_var,
            value="download",
            command=self._on_operacao_change,
        )
        rb.pack(anchor=tk.W, pady=self._ITEM_PADY)
        self._op_radios.append(rb)

        rb = ttk.Radiobutton(
            frame,
            text="Atualizar arquivos de leitura do dashboard",
            variable=self.op_var,
            value="dashboard",
            command=self._on_operacao_change,
        )
        rb.pack(anchor=tk.W, pady=self._ITEM_PADY)
        self._op_radios.append(rb)

        # No SAD a operação não se aplica: o ZIP atualiza dashboard e
        # download juntos (exibido por _on_camada_tab_change).
        self._op_sad_hint = ttk.Label(
            frame,
            text="SAD: dashboard e download são atualizados juntos a partir do ZIP.",
            style="Dim.TLabel",
            wraplength=300,
        )

    # Cada camada (dataset) vira sua própria aba, com o Tipo de dado e o
    # Período específicos dela. "opts" define quais campos de período essa
    # camada precisa (nenhum = só Ano).
    _CAMADAS = [
        ("sad", "SAD", {"show_month": True}),
        ("floreser", "Floreser", {}),
        ("ameaca_pressao", "Ameaça & Pressão", {"show_quarter": True}),
        ("simex", "SIMEX", {}),
    ]
    _QUARTER_LABELS = ["1 (Jan–Mar)", "2 (Abr–Jun)", "3 (Jul–Set)", "4 (Out–Dez)"]

    def _init_period_vars(self) -> None:
        if hasattr(self, "year_var"):
            return
        today = date.today()
        self.year_var = tk.StringVar(value=str(today.year))
        self.month_var = tk.StringVar(value=str(today.month))
        current_q = (today.month - 1) // 3 + 1
        self.quarter_var = tk.StringVar(value=self._QUARTER_LABELS[current_q - 1])

    def _build_camadas_section(self, parent: ttk.Frame) -> None:
        """Cria a aba de cada camada (dataset), cada uma com seu próprio
        Tipo de dado e Período. Trocar de aba já seleciona a camada."""
        ttk.Label(parent, text="Camada (dataset)", style="Dim.TLabel").pack(
            anchor=tk.W, pady=(0, 4)
        )

        self.dataset_var = tk.StringVar(value=self._CAMADAS[0][0])
        self._init_period_vars()
        self._fmt_radios: list = []
        self._camada_slugs = [slug for slug, _label, _opts in self._CAMADAS]
        self._arquivo_vars: dict = {}
        self._arquivo_dicas: dict = {}

        camadas_nb = ttk.Notebook(parent)
        camadas_nb.pack(fill=tk.X, pady=(0, self._SECTION_GAP[1]))

        for slug, label, opts in self._CAMADAS:
            tab = ttk.Frame(camadas_nb, padding=(10, 8))
            camadas_nb.add(tab, text=label)
            if slug == "sad":
                # O SAD sempre gera os 3 formatos a partir do ZIP recebido,
                # então não tem "Tipo de dado".
                self._build_sad_zip_section(tab)
                self._build_period_fields(tab, **opts)
                self._build_sad_dashboard_section(tab)
                self._build_sad_meses_section(tab)
            else:
                self._build_format_section(tab)
                self._build_period_fields(tab, **opts)
                self._build_arquivo_section(tab, slug, label)

        camadas_nb.bind("<<NotebookTabChanged>>", self._on_camada_tab_change)
        self._camadas_nb = camadas_nb

        # A dica de cada aba mostra o formato esperado e o nome no S3
        for var in (self.fmt_var, self.year_var, self.quarter_var):
            var.trace_add("write", lambda *_: self._atualizar_dicas_arquivo())

    def _on_camada_tab_change(self, _event=None) -> None:
        idx = self._camadas_nb.index("current")
        self.dataset_var.set(self._camada_slugs[idx])

        # O Tipo de dado é compartilhado entre as abas: acompanha o arquivo
        # já escolhido na aba aberta
        self._sincronizar_formato_com_arquivo(self.dataset_var.get())

        # O SAD não usa "Operação": dashboard e download saem juntos do ZIP
        sad = self.dataset_var.get() == "sad"
        for rb in self._op_radios:
            rb.configure(state="disabled" if sad else "normal")
        if sad:
            self._op_sad_hint.pack(anchor=tk.W, pady=self._ITEM_PADY)
        else:
            self._op_sad_hint.pack_forget()

        self._update_tutorial()

    def _build_format_section(self, parent: ttk.Frame) -> None:
        frame = ttk.LabelFrame(parent, text="Tipo de dado", padding=self._LF_PADDING)
        frame.pack(fill=tk.X, pady=(0, 6))

        if not hasattr(self, "fmt_var"):
            self.fmt_var = tk.StringVar(value="shapefile")

        # Horizontal em vez de empilhado: economiza altura, já que a aba
        # inteira precisa caber sem rolagem junto com as outras seções.
        for fmt in ("shapefile", "csv", "geojson"):
            rb = ttk.Radiobutton(frame, text=fmt, variable=self.fmt_var, value=fmt)
            rb.pack(side=tk.LEFT, padx=(0, 14))
            self._fmt_radios.append((fmt, rb))

    def _build_period_fields(
        self, parent: ttk.Frame, *, show_month: bool = False, show_quarter: bool = False
    ) -> None:
        frame = ttk.LabelFrame(parent, text="Período", padding=self._LF_PADDING)
        frame.pack(fill=tk.X, pady=(0, 6))

        # Todos os campos de período numa única linha (em vez de uma linha
        # por campo) para reduzir a altura total da aba.
        col = 0
        ttk.Label(frame, text="Ano:").grid(
            row=0, column=col, sticky=tk.W, padx=(0, 6), pady=3
        )
        col += 1
        ttk.Spinbox(
            frame, from_=2000, to=2100, textvariable=self.year_var, width=8
        ).grid(row=0, column=col, sticky=tk.W, padx=(0, 14), pady=3)
        col += 1

        if show_month:
            ttk.Label(frame, text="Mês:").grid(
                row=0, column=col, sticky=tk.W, padx=(0, 6), pady=3
            )
            col += 1
            ttk.Combobox(
                frame,
                values=[str(i) for i in range(1, 13)],
                textvariable=self.month_var,
                width=6,
                state="readonly",
            ).grid(row=0, column=col, sticky=tk.W, padx=(0, 14), pady=3)
            col += 1

        if show_quarter:
            ttk.Label(frame, text="Trimestre:").grid(
                row=0, column=col, sticky=tk.W, padx=(0, 6), pady=3
            )
            col += 1
            ttk.Combobox(
                frame,
                values=self._QUARTER_LABELS,
                textvariable=self.quarter_var,
                width=14,
                state="readonly",
            ).grid(row=0, column=col, sticky=tk.W, pady=3)
            col += 1

    # Arquivo escolhido nas abas Floreser, Ameaça & Pressão e SIMEX
    _FORMATO_POR_EXTENSAO = {
        ".zip": "shapefile",
        ".csv": "csv",
        ".geojson": "geojson",
        ".json": "geojson",
    }
    _NOME_FORMATO = {
        "shapefile": "Shapefile (.zip)",
        "csv": "CSV (.csv)",
        "geojson": "GeoJSON (.geojson)",
    }

    def _build_arquivo_section(self, parent: ttk.Frame, slug: str, label: str) -> None:
        """Seleção do arquivo a enviar nas abas Floreser, Ameaça & Pressão e
        SIMEX. O arquivo é lido de onde estiver e vai para o S3 com o nome
        padrão do dataset e período."""
        frame = ttk.LabelFrame(parent, text="Arquivo", padding=self._LF_PADDING)
        frame.pack(fill=tk.X, pady=(0, 0))

        var = tk.StringVar(value="")
        self._arquivo_vars[slug] = var
        row_file = ttk.Frame(frame)
        row_file.pack(fill=tk.X, pady=2)
        ttk.Entry(row_file, textvariable=var, state="readonly").pack(
            side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 6)
        )
        ttk.Button(
            row_file,
            text="Selecionar…",
            command=lambda: self._selecionar_arquivo(slug, label),
        ).pack(side=tk.LEFT)

        dica = tk.Text(
            frame,
            height=2,
            wrap=tk.WORD,
            state="disabled",
            bg=self._BG2,
            fg=self._FG_DIM,
            relief=tk.FLAT,
            borderwidth=0,
            font=(_FONTE_MONO, 8),
        )
        dica.pack(fill=tk.X, pady=(2, 0))
        self._arquivo_dicas[slug] = dica

    def _dashboard_ap(self, slug: str) -> bool:
        """No dashboard, A&P recebe o GeoJSON do trimestre (ou ZIP com GeoJSONs)."""
        return slug == "ameaca_pressao" and self.op_var.get() == "dashboard"

    def _selecionar_arquivo(self, slug: str, label: str) -> None:
        if self._dashboard_ap(slug):
            tipos = [
                ("GeoJSON do trimestre ou ZIP com GeoJSONs", "*.geojson *.json *.zip")
            ]
        else:
            tipos = [
                ("GeoJSON, CSV ou Shapefile (.zip)", "*.geojson *.json *.csv *.zip")
            ]
        path = filedialog.askopenfilename(
            title=f"Selecione o arquivo — {label}",
            filetypes=tipos + [("Todos os arquivos", "*.*")],
        )
        if not path:
            return
        arquivo = Path(path)
        erro = self._validar_arquivo(slug, arquivo, ajustar_formato=True)
        if erro:
            messagebox.showerror("Arquivo inválido", erro)
            return
        self._arquivo_vars[slug].set(str(arquivo))
        self._atualizar_dicas_arquivo()

    def _validar_arquivo(
        self, slug: str, arquivo: Path, ajustar_formato: bool = False
    ) -> str | None:
        """Confere se o arquivo serve para a Operação e o Tipo de dado atuais.
        Com ajustar_formato=True, o Tipo de dado passa a seguir a extensão do
        arquivo. Retorna a mensagem de erro, ou None se estiver tudo certo."""
        ext = arquivo.suffix.lower()
        if self._dashboard_ap(slug):
            if ext not in (".geojson", ".json", ".zip"):
                return (
                    "No dashboard de Ameaça & Pressão, selecione o GeoJSON do "
                    "trimestre (ou um ZIP com GeoJSONs)."
                )
            return None
        fmt = self._FORMATO_POR_EXTENSAO.get(ext)
        if fmt is None:
            return "Selecione um arquivo .geojson, .csv ou .zip (shapefile)."
        if fmt == "shapefile" and self.op_var.get() == "dashboard":
            return (
                "Shapefile (.zip) não é usado nos arquivos de leitura do dashboard. "
                "Selecione um .geojson ou .csv."
            )
        if ajustar_formato:
            self.fmt_var.set(fmt)
        elif fmt != self.fmt_var.get():
            return (
                f"O arquivo {arquivo.name} é {self._NOME_FORMATO[fmt]}, mas o Tipo de "
                f"dado selecionado é {self._NOME_FORMATO[self.fmt_var.get()]}."
            )
        return None

    def _sincronizar_formato_com_arquivo(self, slug: str) -> None:
        var = self._arquivo_vars.get(slug)
        if var is None or not var.get() or self._dashboard_ap(slug):
            return
        fmt = self._FORMATO_POR_EXTENSAO.get(Path(var.get()).suffix.lower())
        if fmt and not (fmt == "shapefile" and self.op_var.get() == "dashboard"):
            self.fmt_var.set(fmt)

    def _atualizar_dicas_arquivo(self) -> None:
        """Mostra, em cada aba, o que é esperado e para onde o arquivo vai no S3."""
        dashboard = self.op_var.get() == "dashboard"
        for slug, dica in self._arquivo_dicas.items():
            cfg = DATASETS[slug]
            fmt = self.fmt_var.get()
            if dashboard and slug == "ameaca_pressao":
                esperado = "GeoJSON do trimestre (ou ZIP com GeoJSONs)"
                ano = self.year_var.get().strip() or "AAAA"
                destino = (
                    f"{prefixo_dashboard(cfg, 'geojson')}"
                    f"ameaca_e_pressao_{ano}_{{categoria}}_{{classe}}.geojson"
                )
            elif dashboard and slug == "simex":
                esperado = f"{self._NOME_FORMATO[fmt]} unificado do ano"
                nome = (
                    "simex_amazonia_PAMT_{camada}.csv"
                    if fmt == "csv"
                    else "simex_amz_PAMTM_{camada}.geojson"
                )
                destino = prefixo_dashboard(cfg, fmt) + nome
            else:
                esperado = self._NOME_FORMATO[fmt]
                try:
                    periodo = descobrir_periodo(
                        cfg,
                        int(self.year_var.get()),
                        None,
                        self._parse_quarter(self.quarter_var.get()),
                    )
                    nome = montar_nome_arquivo(cfg, periodo, fmt)
                except (ValueError, IndexError):
                    nome = "(período inválido)"
                prefixo = (
                    prefixo_dashboard(cfg, fmt)
                    if dashboard
                    else f"{cfg.s3_root}/{fmt}/"
                )
                destino = prefixo + nome
            dica.configure(state="normal")
            dica.delete("1.0", tk.END)
            dica.insert(tk.END, f"Esperado: {esperado}\nNo S3: {destino}")
            dica.configure(state="disabled")

    def _build_sad_zip_section(self, parent: ttk.Frame) -> None:
        """Seleção do ZIP recebido do SAD — ou de todas as partes, quando o
        download do Google Drive vem dividido. O ZIP é lido de onde estiver:
        nada precisa ser copiado para a pasta base."""
        frame = ttk.LabelFrame(parent, text="ZIP recebido", padding=self._LF_PADDING)
        frame.pack(fill=tk.X, pady=(0, 6))

        self.sad_zips: list = []
        self.sad_zip_var = tk.StringVar(value="")
        row_file = ttk.Frame(frame)
        row_file.pack(fill=tk.X, pady=2)
        ttk.Entry(row_file, textvariable=self.sad_zip_var, state="readonly").pack(
            side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 6)
        )
        ttk.Button(
            row_file, text="Selecionar…", command=self._sad_selecionar_zips
        ).pack(side=tk.LEFT)

        self.sad_zip_status = tk.Text(
            frame,
            height=3,
            wrap=tk.WORD,
            state="disabled",
            bg=self._BG2,
            fg=self._FG_DIM,
            relief=tk.FLAT,
            borderwidth=0,
            font=(_FONTE_MONO, 8),
        )
        self.sad_zip_status.pack(fill=tk.X, pady=(2, 0))
        self._sad_mostrar_status("Nenhum ZIP selecionado.")

    def _build_sad_dashboard_section(self, parent: ttk.Frame) -> None:
        frame = ttk.LabelFrame(
            parent,
            text="CSVs do dashboard (dashboard/sad/csv/)",
            padding=self._LF_PADDING,
        )
        frame.pack(fill=tk.X, pady=(0, 6))

        self.sad_dashboard_var = tk.StringVar(value="todos")
        ttk.Radiobutton(
            frame,
            text="Todos os dados do ZIP",
            variable=self.sad_dashboard_var,
            value="todos",
        ).pack(anchor=tk.W, pady=self._ITEM_PADY)
        ttk.Radiobutton(
            frame,
            text="Só o mês do Período",
            variable=self.sad_dashboard_var,
            value="mes",
        ).pack(anchor=tk.W, pady=self._ITEM_PADY)

    def _build_sad_meses_section(self, parent: ttk.Frame) -> None:
        frame = ttk.LabelFrame(
            parent, text="Arquivos mensais de download (sad/)", padding=self._LF_PADDING
        )
        frame.pack(fill=tk.X, pady=(0, 0))

        self.sad_meses_var = tk.StringVar(value="mes")
        ttk.Radiobutton(
            frame, text="Só o mês do Período", variable=self.sad_meses_var, value="mes"
        ).pack(anchor=tk.W, pady=self._ITEM_PADY)
        ttk.Radiobutton(
            frame,
            text="Todos os meses do ZIP (bem mais demorado)",
            variable=self.sad_meses_var,
            value="todos",
        ).pack(anchor=tk.W, pady=self._ITEM_PADY)

    def _sad_selecionar_zips(self) -> None:
        paths = filedialog.askopenfilenames(
            title=(
                "Selecione o ZIP do SAD (ou todas as partes, se o download veio "
                "dividido)"
            ),
            filetypes=[("ZIP", "*.zip"), ("Todos os arquivos", "*.*")],
        )
        if not paths:
            return
        self._sad_carregar_zips([Path(p) for p in paths])

    def _sad_carregar_zips(self, zips: list) -> None:
        try:
            resumo = inspecionar_zips_sad(zips)
        except Exception as exc:  # ZIP corrompido, sem permissão…
            messagebox.showerror("ZIP inválido", f"Não foi possível ler o ZIP:\n{exc}")
            return
        if not resumo["camadas"]:
            messagebox.showerror(
                "ZIP sem camadas do SAD",
                "Nenhum arquivo no padrão alertas_sad_{tipo}_..._{camada}.shp "
                "foi encontrado no ZIP selecionado.",
            )
            return

        self.sad_zips = zips
        self.sad_zip_var.set("; ".join(z.name for z in zips))

        # Sugere o último mês do ZIP para os arquivos mensais
        (ano_ini, mes_ini), (ano_fim, mes_fim) = resumo["inicio"], resumo["fim"]
        self.year_var.set(str(ano_fim))
        self.month_var.set(str(mes_fim))

        tipos = ", ".join(sorted({t for t, _c in resumo["camadas"]}))
        camadas = ", ".join(sorted({c for _t, c in resumo["camadas"]}))
        self._sad_mostrar_status(
            f"{len(resumo['camadas'])} camada(s) — {tipos}\n"
            f"{camadas}\n"
            f"Período: {mes_ini:02d}/{ano_ini} a {mes_fim:02d}/{ano_fim}"
        )

    def _sad_mostrar_status(self, texto: str) -> None:
        txt = self.sad_zip_status
        txt.configure(state="normal")
        txt.delete("1.0", tk.END)
        txt.insert(tk.END, texto)
        txt.configure(state="disabled")

    def _build_aws_section(self, parent: ttk.Frame) -> None:
        # A própria aba "Configuração AWS" já serve de título da seção.
        frame = parent
        frame.columnconfigure(1, weight=1)

        # Access Key / Secret Key / Região / Bucket / Pasta S3 são só
        # leitura (vêm sempre do .env, local ou empacotado no .exe, e não
        # são editáveis na interface) e mascaradas na tela (show="•"),
        # para não expor esses valores a quem olhar/printar a janela.
        ttk.Label(frame, text="Access Key:").grid(
            row=0, column=0, sticky=tk.W, padx=(0, 8), pady=3
        )
        self.access_key_var = tk.StringVar(value=os.getenv("ACCESS_KEY", ""))
        ttk.Entry(
            frame, textvariable=self.access_key_var, show="•", state="readonly"
        ).grid(row=0, column=1, columnspan=2, sticky=tk.EW, pady=3)

        # Secret Key
        ttk.Label(frame, text="Secret Key:").grid(
            row=1, column=0, sticky=tk.W, padx=(0, 8), pady=3
        )
        self.secret_key_var = tk.StringVar(value=os.getenv("PRIVATE_KEY", ""))
        ttk.Entry(
            frame, textvariable=self.secret_key_var, show="•", state="readonly"
        ).grid(row=1, column=1, columnspan=2, sticky=tk.EW, pady=3)

        # Região
        ttk.Label(frame, text="Região:").grid(
            row=2, column=0, sticky=tk.W, padx=(0, 8), pady=3
        )
        self.region_var = tk.StringVar(value=aws_region())
        ttk.Entry(frame, textvariable=self.region_var, show="•", state="readonly").grid(
            row=2, column=1, columnspan=2, sticky=tk.EW, pady=3
        )

        # Bucket
        ttk.Label(frame, text="Bucket S3:").grid(
            row=3, column=0, sticky=tk.W, padx=(0, 8), pady=3
        )
        self.bucket_var = tk.StringVar(value="imazongeo3-web")
        ttk.Entry(frame, textvariable=self.bucket_var, show="•", state="readonly").grid(
            row=3, column=1, columnspan=2, sticky=tk.EW, pady=3
        )

        # Pasta base
        ttk.Label(frame, text="Pasta base:").grid(
            row=4, column=0, sticky=tk.W, padx=(0, 8), pady=3
        )
        self.base_dir_var = tk.StringVar(value="dados")
        ttk.Entry(frame, textvariable=self.base_dir_var).grid(
            row=4, column=1, sticky=tk.EW, pady=3
        )
        ttk.Button(frame, text="…", width=3, command=self._browse_base_dir).grid(
            row=4, column=2, padx=(4, 0), pady=3
        )

        # Pasta S3 ativa (dashboard)
        ttk.Label(frame, text="Pasta S3:").grid(
            row=5, column=0, sticky=tk.W, padx=(0, 8), pady=3
        )
        self.s3_prefix_var = tk.StringVar(value=S3_DASHBOARD_BASE or "(raiz do bucket)")
        ttk.Entry(
            frame, textvariable=self.s3_prefix_var, show="•", state="readonly"
        ).grid(row=5, column=1, columnspan=2, sticky=tk.EW, pady=3)

    def _build_options_section(self, parent: ttk.Frame) -> None:
        # A própria aba "Opções" já serve de título da seção.
        frame = parent

        ttk.Label(frame, text="Modo de execução", style="Dim.TLabel").pack(
            anchor=tk.W, pady=(0, 4)
        )

        # Modo de execução (radio)
        self.modo_var = tk.StringVar(value="simulacao_completa")
        ttk.Radiobutton(
            frame,
            text="Simulação completa (sem AWS)",
            variable=self.modo_var,
            value="simulacao_completa",
        ).pack(anchor=tk.W, pady=self._ITEM_PADY)
        ttk.Radiobutton(
            frame,
            text="Dry-run rápido (só loga)",
            variable=self.modo_var,
            value="dry_run",
        ).pack(anchor=tk.W, pady=self._ITEM_PADY)
        ttk.Radiobutton(
            frame,
            text="Upload real para S3",
            variable=self.modo_var,
            value="real",
        ).pack(anchor=tk.W, pady=self._ITEM_PADY)

        self.verbose_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            frame,
            text="Modo verboso (DEBUG)",
            variable=self.verbose_var,
        ).pack(anchor=tk.W, pady=(self._ITEM_PADY + 3, self._ITEM_PADY))

    def _build_log_section(self, parent: ttk.Frame) -> None:
        # A própria aba "Log de execução" já serve de título da seção.
        frame = parent

        self.log_text = scrolledtext.ScrolledText(
            frame,
            state="disabled",
            wrap=tk.WORD,
            bg=self._LOG_BG,
            fg="#c9d1d9",
            font=(_FONTE_MONO, 9),
            insertbackground="white",
            relief=tk.FLAT,
            borderwidth=0,
            selectbackground="#264f78",
        )
        self.log_text.pack(fill=tk.BOTH, expand=True)

        # Tags de cor por nível
        self.log_text.tag_configure("INFO", foreground=self._BLUE)
        self.log_text.tag_configure("WARNING", foreground=self._YELLOW)
        self.log_text.tag_configure("ERROR", foreground=self._RED)
        self.log_text.tag_configure("DEBUG", foreground=self._FG_DIM)
        self.log_text.tag_configure("SUCCESS", foreground=self._GREEN)

    def _build_footer(self) -> None:
        ttk.Separator(self, orient="horizontal").pack(fill=tk.X)
        f = ttk.Frame(self, padding=(16, 8, 16, 12))
        f.pack(fill=tk.X)

        self.run_btn = ttk.Button(
            f, text="▶  Executar Upload", style="Run.TButton", command=self._run_upload
        )
        self.run_btn.pack(side=tk.LEFT, padx=(0, 8))

        ttk.Button(f, text="Limpar log", command=self._clear_log).pack(side=tk.LEFT)

        self.status_var = tk.StringVar(value="Pronto.")
        ttk.Label(f, textvariable=self.status_var, style="Dim.TLabel").pack(
            side=tk.RIGHT
        )

    # ------------------------------------------------------------------
    # Logging em tempo real
    # ------------------------------------------------------------------

    def _setup_logging(self) -> None:
        self._log_queue: queue.Queue[str] = queue.Queue()
        handler = _QueueHandler(self._log_queue)
        handler.setFormatter(logging.Formatter(LOG_FORMAT, "%H:%M:%S"))
        root = logging.getLogger()
        root.handlers.clear()
        root.addHandler(handler)
        root.setLevel(logging.INFO)
        self._poll_log()

    def _poll_log(self) -> None:
        while True:
            try:
                msg = self._log_queue.get_nowait()
            except queue.Empty:
                break
            self._append_log(msg)
        self.after(100, self._poll_log)

    def _append_log(self, msg: str) -> None:
        self.log_text.configure(state="normal")
        tag = "INFO"
        if "[WARNING]" in msg:
            tag = "WARNING"
        elif "[ERROR]" in msg or "[CRITICAL]" in msg:
            tag = "ERROR"
        elif "[DEBUG]" in msg:
            tag = "DEBUG"
        if "OK:" in msg or "Concluído" in msg:
            tag = "SUCCESS"
        self.log_text.insert(tk.END, msg + "\n", tag)
        self.log_text.configure(state="disabled")
        self.log_text.see(tk.END)

    def _clear_log(self) -> None:
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", tk.END)
        self.log_text.configure(state="disabled")

    # ------------------------------------------------------------------
    # Lógica de controle
    # ------------------------------------------------------------------

    # Cada camada (dataset) tem um guia próprio para as duas operações
    # possíveis (download / dashboard). Estrutura de cada entrada:
    #   titulo        — nome da camada + periodicidade
    #   descricao     — o que é essa camada, em 1 frase
    #   arquivo_local — onde e como o arquivo deve estar na pasta base
    #   passos        — passo a passo do que o programa faz, em ordem
    #   caminho_s3    — para onde o arquivo vai no bucket
    _TUTORIAL_SAD = {
        "titulo": "SAD · Dashboard + Download  (mensal)",
        "descricao": "Alertas de desmatamento e degradação florestal na Amazônia "
        "Legal, por município, assentamento, terra indígena e unidade "
        "de conservação.",
        "arquivo_local": "ZIP recebido do SAD, em qualquer pasta (se o download veio "
        "dividido em partes, selecione todas).\n"
        "Dentro: alertas_sad_{tipo}_MM_AAAA_MM_AAAA_{camada}.shp",
        "passos": [
            "Selecione o ZIP na aba SAD. O programa mostra as camadas "
            "encontradas e preenche o Período com o último mês do ZIP.",
            "Dashboard: para cada tipo e camada, gera o CSV de atributos "
            "com todos os dados do ZIP ou só o mês do Período, baixa o CSV "
            "acumulado do S3, substitui esses meses e reenvia.",
            "Download: separa os alertas por ano e mês e gera um ZIP por "
            "formato (GeoJSON, CSV e Shapefile), com um arquivo por tipo e "
            "camada.",
            "Os arquivos mensais saem só para o mês do Período ou para "
            "todos os meses do ZIP (bem mais demorado).",
        ],
        "caminho_s3": "dashboard/sad/csv/alertas_sad_{tipo}_{camada}.csv\n"
        "sad/{geojson|csv|shapefile}/sad_{ano}_{mes}.zip",
    }

    _TUTORIAL = {
        # ── SAD ── dashboard e download saem juntos do mesmo ZIP ────────
        ("sad", "download"): _TUTORIAL_SAD,
        ("sad", "dashboard"): _TUTORIAL_SAD,
        # ── Floreser ─────────────────────────────────────────────────────
        ("floreser", "download"): {
            "titulo": "Floreser · Download  (anual)",
            "descricao": "Dados anuais do Floreser.",
            "arquivo_local": "Arquivo escolhido na aba Floreser, em qualquer pasta",
            "passos": [
                "Selecione na aba o arquivo do ano (GeoJSON, CSV ou "
                "Shapefile .zip). O Tipo de dado acompanha a extensão.",
                "O arquivo do formato escolhido é enviado diretamente ao "
                "S3, sem baixar nem mesclar com nenhuma versão anterior.",
            ],
            "caminho_s3": "floreser/{fmt}/floreser_{ano}.{ext}",
        },
        ("floreser", "dashboard"): {
            "titulo": "Floreser · Dashboard  (anual)",
            "descricao": "Mesmos arquivos do Download — o Floreser não acumula "
            "histórico entre execuções, então Download e Dashboard "
            "funcionam da mesma forma.",
            "arquivo_local": "Arquivo escolhido na aba Floreser, em qualquer pasta",
            "passos": [
                "Selecione na aba o arquivo do ano (GeoJSON, CSV ou "
                "Shapefile .zip). O Tipo de dado acompanha a extensão.",
                "O arquivo é enviado diretamente ao S3, substituindo "
                "qualquer versão anterior do mesmo ano.",
            ],
            "caminho_s3": "floreser/{fmt}/floreser_{ano}.{ext}",
        },
        # ── Ameaça & Pressão ─────────────────────────────────────────────
        ("ameaca_pressao", "download"): {
            "titulo": "Ameaça & Pressão · Download  (trimestral)",
            "descricao": "Indicadores trimestrais de ameaça e pressão sobre áreas "
            "protegidas.",
            "arquivo_local": (
                "Arquivo do trimestre escolhido na aba Ameaça & Pressão, em "
                "qualquer pasta"
            ),
            "passos": [
                "Selecione na aba o arquivo do trimestre (GeoJSON, CSV ou "
                "Shapefile .zip). O Tipo de dado acompanha a extensão.",
                "O arquivo é enviado diretamente ao S3, sem baixar nem "
                "mesclar com nenhuma versão anterior.",
            ],
            "caminho_s3": (
                "ameaca_e_pressao/{fmt}/ameaca_e_pressao_{Q}_trimestre_{ano}.{ext}"
            ),
        },
        ("ameaca_pressao", "dashboard"): {
            "titulo": "Ameaça & Pressão · Dashboard  (anual, por categoria)",
            "descricao": "Arquivos anuais lidos pelo dashboard, um por categoria e "
            "classe (ameaça ou pressão), com os trimestres do ano.",
            "arquivo_local": "GeoJSON do trimestre (o mesmo do Download) ou ZIP com "
            "GeoJSONs, escolhido na aba Ameaça & Pressão",
            "passos": [
                "Separa as feições por ano, categoria (dado) e classe (class).",
                "Para cada combinação, baixa do S3 o arquivo anual e substitui "
                "os trimestres enviados, mantendo os demais.",
                "Reenvia os arquivos anuais: enviar de novo o mesmo trimestre "
                "não duplica os dados.",
            ],
            "caminho_s3": (
                "dashboard/ap/geojson/"
                "ameaca_e_pressao_{ano}_{categoria}_{classe}.geojson"
            ),
        },
        # ── SIMEX ────────────────────────────────────────────────────────
        ("simex", "download"): {
            "titulo": "SIMEX · Download  (anual)",
            "descricao": "Dados anuais de exploração madeireira (SIMEX).",
            "arquivo_local": "Arquivo escolhido na aba SIMEX, em qualquer pasta",
            "passos": [
                "Selecione na aba o arquivo do ano (GeoJSON, CSV ou "
                "Shapefile .zip). O Tipo de dado acompanha a extensão.",
                "O arquivo é enviado diretamente ao S3, sem baixar nem "
                "mesclar com nenhuma versão anterior.",
            ],
            "caminho_s3": "simex/{fmt}/simex_unificado_{ano}.{ext}",
        },
        ("simex", "dashboard"): {
            "titulo": "SIMEX · Dashboard  (por camada, todos os anos)",
            "descricao": "Arquivos por camada lidos pelo dashboard (assentamentos, "
            "imóveis rurais, municípios, terras não destinadas, TI e UC), "
            "com todos os anos.",
            "arquivo_local": (
                "Arquivo unificado do ano (o mesmo do Download), em CSV ou "
                "GeoJSON, escolhido na aba SIMEX"
            ),
            "passos": [
                "Separa os registros por camada, pela coluna origem_arquivo "
                "ou source_layer.",
                "Para cada camada, baixa do S3 o arquivo do dashboard e "
                "substitui os anos enviados, mantendo os demais.",
                "O CSV atualiza os CSVs do dashboard e o GeoJSON atualiza os "
                "mapas: envie os dois para atualizar tudo.",
            ],
            "caminho_s3": "dashboard/simex/csv/simex_amazonia_PAMT_{camada}.csv\n"
            "dashboard/simex/geojson/simex_amz_PAMTM_{camada}.geojson",
        },
    }

    def _build_tutorial_section(self, parent: ttk.Frame) -> None:
        # A própria aba "Guia de uso" já serve de título da seção.
        frame = parent

        self.tutorial_text = tk.Text(
            frame,
            state="disabled",
            wrap=tk.WORD,
            bg="#0d1117",
            fg=self._FG,
            font=(_FONTE, 9),
            relief=tk.FLAT,
            borderwidth=0,
            cursor="arrow",
            padx=8,
            pady=6,
            spacing1=1,
        )
        self.tutorial_text.pack(fill=tk.BOTH, expand=True)

        # Tags de formatação
        self.tutorial_text.tag_configure(
            "titulo", foreground=self._ACCENT, font=(_FONTE, 11, "bold"), spacing3=6
        )
        self.tutorial_text.tag_configure(
            "descricao",
            foreground=self._FG_DIM,
            font=(_FONTE, 9, "italic"),
            spacing3=10,
        )
        self.tutorial_text.tag_configure(
            "label", foreground=self._YELLOW, font=(_FONTE, 9, "bold"), spacing1=4
        )
        self.tutorial_text.tag_configure(
            "caminho",
            foreground=self._FG,
            font=(_FONTE_MONO, 9),
            lmargin1=16,
            lmargin2=16,
            spacing3=10,
        )
        self.tutorial_text.tag_configure(
            "passo_num",
            foreground=self._GREEN,
            font=(_FONTE, 9, "bold"),
            lmargin1=4,
            lmargin2=20,
        )
        self.tutorial_text.tag_configure(
            "passo_texto",
            foreground=self._FG,
            font=(_FONTE, 9),
            lmargin1=4,
            lmargin2=20,
            spacing3=6,
        )

        self._update_tutorial()

    def _update_tutorial(self) -> None:
        if not hasattr(self, "tutorial_text"):
            return
        chave = (self.dataset_var.get(), self.op_var.get())
        dados = self._TUTORIAL.get(chave, {})
        t = self.tutorial_text
        t.configure(state="normal")
        t.delete("1.0", tk.END)

        t.insert(tk.END, dados.get("titulo", "") + "\n", "titulo")

        if dados.get("descricao"):
            t.insert(tk.END, dados["descricao"] + "\n", "descricao")

        if dados.get("arquivo_local"):
            t.insert(tk.END, "Arquivo local esperado\n", "label")
            t.insert(tk.END, dados["arquivo_local"] + "\n", "caminho")

        if dados.get("passos"):
            t.insert(tk.END, "Como funciona\n", "label")
            for i, passo in enumerate(dados["passos"], start=1):
                t.insert(tk.END, f"{i}. ", "passo_num")
                t.insert(tk.END, passo + "\n", "passo_texto")
            t.insert(tk.END, "\n")

        if dados.get("caminho_s3"):
            t.insert(tk.END, "Caminho final no S3\n", "label")
            t.insert(tk.END, dados["caminho_s3"] + "\n", "caminho")

        t.configure(state="disabled")

    def _on_operacao_change(self) -> None:
        only_dashboard = self.op_var.get() == "dashboard"
        allowed = (
            ("csv", "geojson") if only_dashboard else ("shapefile", "csv", "geojson")
        )

        for fmt, rb in self._fmt_radios:
            rb.configure(state="normal" if fmt in allowed else "disabled")

        # Se o formato atual ficou desabilitado, seleciona o primeiro permitido
        if self.fmt_var.get() not in allowed:
            self.fmt_var.set(allowed[0])

        self._sincronizar_formato_com_arquivo(self.dataset_var.get())
        self._atualizar_dicas_arquivo()
        self._update_tutorial()

    def _browse_base_dir(self) -> None:
        path = filedialog.askdirectory(title="Selecione a pasta base dos dados")
        if path:
            self.base_dir_var.set(path)

    def _parse_quarter(self, raw: str) -> int:
        """Extrai número do trimestre (ex: '3 (Jul–Set)' → 3)."""
        return int(raw.split()[0])

    def _run_upload(self) -> None:
        dataset = self.dataset_var.get()
        bucket = self.bucket_var.get().strip()
        base_dir_str = self.base_dir_var.get().strip()
        modo = self.modo_var.get()
        verbose = self.verbose_var.get()
        selected_formats = [self.fmt_var.get()]
        op_download = self.op_var.get() == "download"
        op_dashboard = self.op_var.get() == "dashboard"

        # O SAD não usa "Operação": dashboard e download saem juntos do ZIP
        sad_zips = list(self.sad_zips)
        sad_todos_meses = self.sad_meses_var.get() == "todos"
        sad_dashboard_todos = self.sad_dashboard_var.get() == "todos"
        if dataset == "sad":
            op_download = op_dashboard = False

        # Credenciais AWS são só leitura na GUI (vêm do .env, local ou
        # empacotado no .exe) — aqui só garantimos que create_s3_client()
        # as encontre no ambiente.
        access_key = self.access_key_var.get().strip()
        secret_key = self.secret_key_var.get().strip()
        region = self.region_var.get().strip() or "us-east-1"
        os.environ["ACCESS_KEY"] = access_key
        os.environ["PRIVATE_KEY"] = secret_key
        os.environ["AWS_REGION"] = region

        # Validações
        if not bucket:
            messagebox.showerror("Campo obrigatório", "Informe o nome do bucket S3.")
            return

        if dataset == "sad" and not sad_zips:
            messagebox.showerror(
                "ZIP não selecionado", "Selecione o ZIP recebido do SAD na aba SAD."
            )
            return

        arquivo = None
        if dataset != "sad":
            caminho = self._arquivo_vars[dataset].get()
            if not caminho:
                messagebox.showerror(
                    "Arquivo não selecionado",
                    "Selecione, na aba da camada, o arquivo a enviar.",
                )
                return
            arquivo = Path(caminho)
            if not arquivo.is_file():
                messagebox.showerror(
                    "Arquivo não encontrado", f"O arquivo não existe mais:\n{arquivo}"
                )
                return
            erro = self._validar_arquivo(dataset, arquivo)
            if erro:
                messagebox.showerror("Arquivo inválido", erro)
                return

        # Upload real é o único modo que fala de verdade com a AWS (dry-run e
        # simulação não chamam create_s3_client). Sem isso, um .exe rodando em
        # outro computador sem .env falharia lá na frente com um erro genérico
        # dentro da thread, em vez de avisar de imediato o que está faltando.
        if modo == "real" and (not access_key or not secret_key):
            messagebox.showerror(
                "Credenciais AWS ausentes",
                "As credenciais AWS (Access Key / Secret Key) não foram\n"
                "carregadas e não podem ser digitadas nesta tela.\n\n"
                "Elas vêm automaticamente do arquivo .env — local, ao lado\n"
                "do programa, ou empacotado dentro do .exe. Configure o .env\n"
                "corretamente (ou gere um novo executável com as credenciais\n"
                "certas) antes de tentar um upload real.",
            )
            return

        try:
            year = int(self.year_var.get())
        except ValueError:
            messagebox.showerror("Valor inválido", "O ano informado é inválido.")
            return

        month: int | None = None
        if dataset == "sad":
            try:
                month = int(self.month_var.get())
            except ValueError:
                messagebox.showerror("Valor inválido", "O mês informado é inválido.")
                return

        quarter: int | None = None
        if dataset == "ameaca_pressao":
            try:
                quarter = self._parse_quarter(self.quarter_var.get())
            except (ValueError, IndexError):
                messagebox.showerror(
                    "Valor inválido", "O trimestre informado é inválido."
                )
                return

        periodo_resumo = None
        if dataset == "sad" and (sad_todos_meses or sad_dashboard_todos):
            mes_ano = f"{month:02d}/{year}"
            todos = "todos os meses do ZIP"
            periodo_resumo = (
                f"dashboard {todos if sad_dashboard_todos else mes_ano}, "
                f"download {todos if sad_todos_meses else mes_ano}"
            )

        if modo == "real":
            if dataset == "sad":
                confirmado = self._confirmar_operacao_sad(
                    sad_zips, year, month, sad_todos_meses, sad_dashboard_todos
                )
            else:
                confirmado = self._confirmar_operacao(
                    dataset,
                    selected_formats,
                    op_download,
                    op_dashboard,
                    year,
                    month,
                    quarter,
                    arquivo,
                )
            if not confirmado:
                return
            if not self._pedir_senha():
                return

        # Bloqueia UI e dispara thread
        self.run_btn.configure(state="disabled")
        self.status_var.set("Executando…")
        logging.getLogger().setLevel(logging.DEBUG if verbose else logging.INFO)

        def _task() -> None:
            try:
                names = [dataset]
                base = Path(base_dir_str).resolve()

                if modo == "simulacao_completa":
                    logging.info(">>> Modo: simulação completa (SimuladorS3)")
                    sim = executar_simulacao(
                        dataset=dataset,
                        bucket=bucket,
                        base_dir=base,
                        year=year,
                        month=month,
                        quarter=quarter,
                        public=True,
                        formats=selected_formats,
                        concatenar=False,
                        op_download=op_download,
                        op_dashboard=op_dashboard,
                        sad_zips=sad_zips,
                        sad_todos_meses=sad_todos_meses,
                        sad_dashboard_todos_meses=sad_dashboard_todos,
                        arquivo=arquivo,
                    )
                    for linha in sim.relatorio().splitlines():
                        logging.info(linha)
                    return

                # dry_run rápido ou upload real
                dry_run = modo == "dry_run"

                if dataset == "sad":
                    logging.info(
                        ">>> SAD: dashboard (CSVs) + arquivos mensais de download"
                    )
                    processar_sad_zip(
                        zips=sad_zips,
                        bucket=bucket,
                        dry_run=dry_run,
                        ano=year,
                        mes=month,
                        todos_meses=sad_todos_meses,
                        public=True,
                        dashboard_todos_meses=sad_dashboard_todos,
                    )

                if op_download:
                    logging.info(">>> Operação: atualizar arquivos de download")
                    for name in names:
                        processar_dataset(
                            nome_dataset=name,
                            base_dir=base,
                            bucket=bucket,
                            year=year,
                            month=month,
                            quarter=quarter,
                            dry_run=dry_run,
                            public=True,
                            formats=selected_formats,
                            concatenar=False,
                            op="download",
                            arquivo=arquivo,
                        )

                if op_dashboard and not dry_run:
                    existentes = self._verificar_dados_existentes_s3(
                        dataset, selected_formats, bucket, year, month, quarter
                    )
                    if existentes:
                        continuar = [None]
                        ev = threading.Event()

                        def _ask_duplicata():
                            nomes = "\n".join(f"  • {k}" for k in existentes)
                            continuar[0] = messagebox.askyesno(
                                "Dados já existentes no S3",
                                "Já foram encontrados dados no S3 para este "
                                f"dataset:\n\n{nomes}\n\n"
                                "Se continuar, os registros do período "
                                "selecionado serão\nsubstituídos pelos novos "
                                "dados (duplicatas removidas automaticamente)."
                                "\n\nDeseja continuar mesmo assim?",
                            )
                            ev.set()

                        self.after(0, _ask_duplicata)
                        ev.wait()
                        if not continuar[0]:
                            logging.info(
                                "Operação cancelada: dados duplicados detectados no S3."
                            )
                            return

                if op_dashboard:
                    logging.info(
                        ">>> Operação: atualizar arquivos de leitura do dashboard"
                    )
                    if "ameaca_pressao" in names:
                        processar_ameaca_pressao_dashboard(
                            base_dir=base,
                            bucket=bucket,
                            year=year,
                            dry_run=dry_run,
                            arquivo=arquivo,
                        )
                    if "simex" in names:
                        processar_simex_dashboard(
                            bucket=bucket,
                            dry_run=dry_run,
                            arquivo=arquivo,
                        )
                    for name in [
                        n for n in names if n not in ("sad", "ameaca_pressao", "simex")
                    ]:
                        processar_dataset(
                            nome_dataset=name,
                            base_dir=base,
                            bucket=bucket,
                            year=year,
                            month=month,
                            quarter=quarter,
                            dry_run=dry_run,
                            public=True,
                            formats=selected_formats,
                            concatenar=True,
                            op="dashboard",
                            arquivo=arquivo,
                        )

                logging.info("=== Concluído com sucesso ===")
                if modo == "real":
                    self.after(
                        0,
                        lambda: self._mostrar_resumo_upload(
                            dataset, year, month, quarter, periodo_resumo
                        ),
                    )
            except Exception as exc:
                logging.error("Erro durante o processamento: %s", exc)
            finally:
                self.after(0, self._on_upload_done)

        threading.Thread(target=_task, daemon=True).start()

    def _on_upload_done(self) -> None:
        self.run_btn.configure(state="normal")
        self.status_var.set("Pronto.")

    def _confirmar_operacao(
        self,
        dataset: str,
        formats: list,
        op_download: bool,
        op_dashboard: bool,
        year: int,
        month: int | None,
        quarter: int | None,
        arquivo: Path | None = None,
    ) -> bool:
        """Exibe resumo da operação e pede confirmação antes do upload real."""
        nomes_ds = {
            "sad": "SAD – Mensal",
            "floreser": "Floreser – Anual",
            "ameaca_pressao": "Ameaça & Pressão – Trimestral",
            "simex": "SIMEX – Anual",
        }
        nomes_fmt = {
            "shapefile": "Shapefile (.zip)",
            "csv": "CSV",
            "geojson": "GeoJSON",
        }
        nome_ds = nomes_ds.get(dataset, dataset)
        nome_fmt = nomes_fmt.get(formats[0], formats[0]) if formats else "—"
        operacao = "Adicionar dado para download" if op_download else "Dashboard"

        if month is not None:
            meses = [
                "Jan",
                "Fev",
                "Mar",
                "Abr",
                "Mai",
                "Jun",
                "Jul",
                "Ago",
                "Set",
                "Out",
                "Nov",
                "Dez",
            ]
            periodo = f"{meses[month - 1]}/{year}"
        elif quarter is not None:
            periodo = f"{year} – T{quarter}"
        else:
            periodo = str(year)

        msg = (
            f"Confirme o upload para AWS S3:\n\n"
            f"  Base de dados : {nome_ds}\n"
            f"  Arquivo       : {arquivo.name if arquivo else '—'}\n"
            f"  Tipo de dado  : {nome_fmt}\n"
            f"  Operação      : {operacao}\n"
            f"  Período       : {periodo}\n\n"
            f"Deseja continuar?"
        )
        return messagebox.askyesno("Confirmar upload", msg)

    def _confirmar_operacao_sad(
        self,
        zips: list,
        year: int,
        month: int,
        todos_meses: bool,
        dashboard_todos_meses: bool,
    ) -> bool:
        """Resume a atualização do SAD e pede confirmação antes do upload real."""
        dashboard = (
            "todos os dados do ZIP"
            if dashboard_todos_meses
            else f"só {month:02d}/{year}"
        )
        mensais = (
            "todos os meses do ZIP" if todos_meses else f"sad_{year}_{month:02d}.zip"
        )
        msg = (
            f"Confirme o upload para AWS S3:\n\n"
            f"  Base de dados : SAD – Mensal\n"
            f"  ZIP           : {', '.join(z.name for z in zips)}\n"
            f"  Dashboard     : dashboard/sad/csv/ — {dashboard}\n"
            f"  Download      : sad/geojson, sad/csv e sad/shapefile — {mensais}\n\n"
            f"Deseja continuar?"
        )
        return messagebox.askyesno("Confirmar upload", msg)

    def _verificar_dados_existentes_s3(
        self,
        dataset: str,
        formats: list,
        bucket: str,
        year: int,
        month: int | None,
        quarter: int | None,
    ) -> list:
        """
        Verifica no S3 se já existem arquivos de dashboard para o dataset/período.
        Retorna lista de chaves (ou prefixos) encontrados.
        Erros de acesso ao S3 são silenciados para não bloquear a operação.
        """
        cfg = DATASETS[dataset]
        periodo = descobrir_periodo(cfg, year, month, quarter)
        try:
            s3 = create_s3_client()
        except Exception:
            return []

        existentes = []
        for fmt in formats:
            if dataset == "floreser":
                nome = montar_nome_arquivo(cfg, periodo, fmt)
                key = prefixo_dashboard(cfg, fmt) + nome
                try:
                    s3.head_object(Bucket=bucket, Key=key)
                    existentes.append(key)
                except Exception:
                    pass
            else:
                # SAD, A&P e SIMEX: verifica se o prefixo de dashboard tem objetos
                prefix = prefixo_dashboard(
                    cfg, "geojson" if dataset == "ameaca_pressao" else fmt
                )
                try:
                    resp = s3.list_objects_v2(Bucket=bucket, Prefix=prefix, MaxKeys=1)
                    if resp.get("KeyCount", 0) > 0:
                        existentes.append(prefix)
                except Exception:
                    pass
        return existentes

    def _pedir_senha(self) -> bool:
        """Solicita senha de upload e valida contra UPLOAD_PASSWORD do .env.
        Retorna True se a senha estiver correta."""
        senha_correta = os.getenv("UPLOAD_PASSWORD", "")
        if not senha_correta:
            messagebox.showerror(
                "Senha não configurada",
                "Defina UPLOAD_PASSWORD no arquivo .env para permitir uploads reais.",
            )
            return False

        senha = simpledialog.askstring(
            "Autenticação",
            "Digite a senha para autorizar o upload real para S3:",
            show="*",
            parent=self,
        )
        if senha is None:  # cancelou
            return False
        if senha != senha_correta:
            messagebox.showerror("Senha incorreta", "Senha inválida. Upload cancelado.")
            return False
        return True

    def _mostrar_resumo_upload(
        self,
        dataset: str,
        year: int,
        month: int | None,
        quarter: int | None,
        periodo: str | None = None,
    ) -> None:
        """Exibe diálogo de confirmação pós-upload com dataset e período."""
        nomes = {
            "sad": "SAD – Mensal",
            "floreser": "Floreser – Anual",
            "ameaca_pressao": "Ameaça & Pressão – Trimestral",
            "simex": "SIMEX – Anual",
        }
        nome_ds = nomes.get(dataset, dataset)

        if periodo is not None:
            pass  # já veio pronto (ex.: SAD com todos os meses do ZIP)
        elif month is not None:
            meses = [
                "Jan",
                "Fev",
                "Mar",
                "Abr",
                "Mai",
                "Jun",
                "Jul",
                "Ago",
                "Set",
                "Out",
                "Nov",
                "Dez",
            ]
            periodo = f"{meses[month - 1]}/{year}"
        elif quarter is not None:
            periodo = f"{year} – T{quarter}"
        else:
            periodo = str(year)

        messagebox.showinfo(
            "Upload concluído",
            f"Upload realizado com sucesso!\n\n"
            f"Base de dados:  {nome_ds}\n"
            f"Período:           {periodo}",
        )


# ---------------------------------------------------------------------------


def main() -> None:
    """Ponto de entrada do comando ``imazongeo-upload-gui``."""
    _carregar_env()
    app = ImazonUploadApp()
    app.mainloop()


if __name__ == "__main__":
    main()
