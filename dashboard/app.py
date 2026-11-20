"""
Como executar, a partir da raiz do projeto:
    streamlit run dashboard/app.py
"""

import streamlit as st

import queries

RAIN_LABELS = {
    "dry": "Seco (0 mm/h)",
    "light": "Chuva fraca (<= 2,5 mm/h)",
    "moderate": "Chuva moderada (<= 10 mm/h)",
    "heavy": "Chuva forte (> 10 mm/h)",
    "unknown": "Sem dado de chuva",
}

SPEED_FLAG_LABELS = {
    "ok": "Válida",
    "first_reading": "1ª leitura",
    "dt_out_of_range": "Intervalo inválido",
    "parked": "Parado",
    "too_fast": "Acima de 80 km/h",
}

st.set_page_config(
    page_title="Impacto da Chuva na Velocidade dos Ônibus (SPTrans)",
    layout="wide",
)


def br(valor, casas=1):
    """Formata numero no padrao brasileiro, com virgula decimal."""
    if valor is None:
        return "N/D"
    texto = f"{valor:,.{casas}f}"
    return texto.replace(",", "@").replace(".", ",").replace("@", ".")


def br_signed(valor, casas=2):
    """Formata numero com sinal explicito no padrao brasileiro."""
    if valor is None:
        return "N/D"
    sinal = "+" if valor > 0 else ""
    return f"{sinal}{br(valor, casas)}"


# ------------------------------------------------------------ abertura
st.title("Impacto da Chuva na Velocidade dos Ônibus (SPTrans)")
st.caption(
    "Camada Gold (`data/gold/project.duckdb`) cruzando telemetria GPS da SPTrans "
    "com estações pluviométricas do CEMADEN."
)

db_path = queries.get_db_path()
if not db_path.exists():
    st.error(
        f"Banco não encontrado em `{db_path}`.\n\n"
        "Rode o pipeline antes:  `python src/pipeline.py`"
    )
    st.stop()

with st.expander("Como ler este painel"):
    st.markdown(
        "- **Classificação da chuva (mm/h na estação mais próxima):** `Seco` (0 mm), "
        "`Chuva fraca` (até 2,5 mm/h), `Chuva moderada` (até 10 mm/h) e `Chuva forte` (> 10 mm/h).\n"
        "- **Impacto da Chuva (Delta):** diferença entre a velocidade na chuva e a média sem chuva "
        "**da mesma linha, mesmo sentido, mesmo trecho (estação mais próxima) e mesma faixa de horário (pico/fora de pico)**."
    )

# ------------------------------------------------------------- filtros
with st.sidebar:
    st.header("Filtros")
    min_readings = st.slider(
        "Mínimo de leituras (seco e chuva)",
        min_value=1,
        max_value=200,
        value=30,
        step=5,
        help="Exige pelo menos N leituras válidas no seco e N na chuva para comparar linhas e categorias.",
    )
    options = queries.get_filter_options(min_readings_for_default=min_readings)

    selected_routes = st.multiselect(
        "Linhas de ônibus",
        options=options["routes"],
        default=options["default_routes"],
        help="Por padrão, apenas linhas com amostra mínima no seco e na chuva vêm marcadas.",
    )
    peak_choice = st.radio(
        "Janela de horário",
        options=["all", "peak", "off_peak"],
        format_func=lambda x: {
            "all": "Todos os horários",
            "peak": "Somente pico (06h-09h, 17h-20h)",
            "off_peak": "Somente fora do pico",
        }[x],
    )
    st.divider()
    st.caption(
        "Os filtros viram parâmetro na consulta SQL (read_only=True), "
        "nunca texto concatenado."
    )

# ------------------------------------------------------------- cartoes
kpis = queries.get_overview_metrics(
    route_ids=selected_routes,
    peak_mode=peak_choice,
    include_unknown=False,
)

c1, c2, c3, c4 = st.columns(4)
c1.metric(
    "Seco (mesmo trecho/horário)",
    f"{br(kpis['dry_avg_speed_kmh'], 2)} km/h" if kpis["dry_avg_speed_kmh"] is not None else "N/D",
    delta=f"base: {br(kpis['dry_readings'], 0)} leituras secas",
    delta_color="off",
    help="Média sem chuva nos mesmos trechos, sentidos e horários em que choveu.",
)
c2.metric(
    "Velocidade na Chuva",
    f"{br(kpis['rain_avg_speed_kmh'], 2)} km/h" if kpis["rain_avg_speed_kmh"] is not None else "N/D",
    delta=f"{br(kpis['rain_readings'], 0)} leituras na chuva",
    delta_color="off",
)
c3.metric(
    "Impacto Médio da Chuva",
    f"{br_signed(kpis['rain_avg_speed_diff_kmh'], 2)} km/h"
    if kpis["rain_avg_speed_diff_kmh"] is not None
    else "N/D",
    delta=f"{br_signed(kpis['rain_avg_speed_diff_pct'], 1)}% vs. seco"
    if kpis["rain_avg_speed_diff_pct"] is not None
    else None,
    delta_color="normal",
)
c4.metric(
    "Amostra Analisada",
    br(kpis["total_ok_readings"], 0),
    delta=f"{kpis['rain_routes_count']} linha(s) com chuva",
    delta_color="off",
)

st.divider()

# ------------------------------------ bloco 1: grafico ao lado da tabela
st.subheader("Impacto por Intensidade da Chuva")
intensity_df = queries.get_speed_by_rain_intensity(
    route_ids=selected_routes,
    peak_mode=peak_choice,
    include_unknown=False,
    min_readings=min_readings,
)

if not intensity_df.empty:
    esq, dir_ = st.columns([3, 2])

    with esq:
        rain_only_df = intensity_df[intensity_df["rain_intensity"] != "dry"].copy()
        if not rain_only_df.empty:
            rain_only_df["Intensidade"] = rain_only_df["rain_intensity"].map(
                lambda x: RAIN_LABELS.get(x, x)
            )
            st.bar_chart(
                rain_only_df,
                x="Intensidade",
                y="avg_diff_from_dry_kmh",
                color="#1B3A5F",
                height=300,
            )
        else:
            st.info(
                "Nenhuma faixa de chuva atingiu o mínimo de leituras para o gráfico de impacto."
            )

    with dir_:
        table_int = intensity_df.copy()
        table_int["rain_intensity"] = table_int["rain_intensity"].map(
            lambda x: RAIN_LABELS.get(x, x)
        )
        st.dataframe(
            table_int,
            hide_index=True,
            use_container_width=True,
            column_config={
                "rain_intensity": st.column_config.TextColumn("Categoria"),
                "readings": st.column_config.NumberColumn("Leituras", format="%d"),
                "avg_speed_kmh": st.column_config.NumberColumn("Média (km/h)", format="%.2f"),
                "median_speed_kmh": st.column_config.NumberColumn("Mediana (km/h)", format="%.2f"),
                "avg_diff_from_dry_kmh": st.column_config.NumberColumn(
                    "Delta vs. Seco (km/h)",
                    format="%.2f",
                    help="Diferença média em relação ao mesmo trecho/horário sem chuva.",
                ),
            },
        )
else:
    st.info(
        f"Nenhuma categoria atingiu o mínimo de {min_readings} leituras para os filtros atuais."
    )

st.divider()

# ------------------------------------ bloco 2: tabela por linha largura total
st.subheader("Comparação por Linha (maior perda → menor)")
route_df = queries.get_speed_by_route(
    route_ids=selected_routes,
    peak_mode=peak_choice,
    include_unknown=False,
    min_readings=min_readings,
)
if not route_df.empty:
    st.dataframe(
        route_df,
        hide_index=True,
        use_container_width=True,
        column_config={
            "route_id": st.column_config.TextColumn("Linha"),
            "route_name": st.column_config.TextColumn("Nome da Linha"),
            "readings": st.column_config.NumberColumn("Total Válidas", format="%d"),
            "dry_readings": st.column_config.NumberColumn("Leituras (Seco)", format="%d"),
            "rain_readings": st.column_config.NumberColumn("Leituras (Chuva)", format="%d"),
            "dry_avg_speed_kmh": st.column_config.NumberColumn(
                "Seco Mesmo Trecho (km/h)", format="%.2f"
            ),
            "rain_avg_speed_kmh": st.column_config.NumberColumn(
                "Média Chuva (km/h)", format="%.2f"
            ),
            "avg_diff_from_dry_kmh": st.column_config.NumberColumn(
                "Impacto (km/h)",
                format="%.2f",
                help="Média da diferença trecho a trecho em relação ao tempo seco.",
            ),
        },
    )
else:
    st.info(f"Nenhuma linha atingiu o mínimo de {min_readings} leituras no seco e na chuva.")

# ---------------------------------------------------- auditoria opcional
with st.expander("Auditar cobertura e leituras individuais"):
    if kpis["unknown_readings"] > 0:
        st.caption(
            f"{br(kpis['unknown_readings'], 0)} leituras válidas estão sem dado de chuva no CEMADEN "
            "para a hora correspondente e não entram nos gráficos acima."
        )
    include_non_ok = st.checkbox("Incluir veículos parados / leituras descartadas", value=False)
    sample_df = queries.get_readings_sample(
        route_ids=selected_routes,
        peak_mode=peak_choice,
        include_unknown=True,
        only_ok_speed=not include_non_ok,
        limit=100,
    )
    if not sample_df.empty:
        display_sample = sample_df.copy()
        display_sample["rain_intensity"] = display_sample["rain_intensity"].map(
            lambda x: RAIN_LABELS.get(x, x)
        )
        display_sample["speed_flag"] = display_sample["speed_flag"].map(
            lambda x: SPEED_FLAG_LABELS.get(x, x)
        )
        st.dataframe(
            display_sample,
            hide_index=True,
            use_container_width=True,
            column_config={
                "observed_at": st.column_config.DatetimeColumn("Horário (SP)"),
                "route_id": st.column_config.TextColumn("Linha"),
                "direction_id": st.column_config.NumberColumn("Sentido", format="%d"),
                "vehicle_id": st.column_config.TextColumn("Veículo"),
                "speed_kmh": st.column_config.NumberColumn("Velocidade (km/h)", format="%.2f"),
                "speed_flag": st.column_config.TextColumn("Status GPS"),
                "station_id": st.column_config.TextColumn("Estação"),
                "station_name": st.column_config.TextColumn("Nome da Estação"),
                "precipitation_mm": st.column_config.NumberColumn("Chuva (mm/h)", format="%.2f"),
                "rain_intensity": st.column_config.TextColumn("Intensidade"),
                "speed_diff_from_dry_avg": st.column_config.NumberColumn(
                    "Delta vs. Seco (km/h)", format="%.2f"
                ),
                "is_peak": st.column_config.CheckboxColumn("Pico"),
            },
        )

# ---------------------------------------------------- rodape de proveniencia
prov = queries.get_provenance_summary()
if prov:
    st.caption(
        f"Fonte: API SPTrans Olho Vivo & CEMADEN | Etapa: `{prov.get('stage')}` | "
        f"Registros na Gold: {br(prov.get('records'), 0)} | Atualizado em: {prov.get('executed_at')}"
    )
else:
    st.caption("Fonte: API SPTrans Olho Vivo & CEMADEN (data/gold/project.duckdb).")
