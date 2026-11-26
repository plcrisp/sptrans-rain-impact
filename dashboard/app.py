"""
Como executar, a partir da raiz do projeto:
    streamlit run dashboard/app.py
"""

import altair as alt
import pandas as pd
import streamlit as st

import queries

MIN_READINGS = 30
COLOR_LOSS = "#C0392B"
COLOR_GAIN = "#2E6B9E"

st.set_page_config(
    page_title="Impacto da Chuva na Velocidade dos Ônibus (SPTrans)",
    layout="wide",
)


def br(valor, casas=1):
    """Formata número no padrão brasileiro, com vírgula decimal."""
    if valor is None or pd.isna(valor):
        return "N/D"
    texto = f"{valor:,.{casas}f}"
    return texto.replace(",", "@").replace(".", ",").replace("@", ".")


def br_signed(valor, casas=2):
    """Formata número com sinal explícito no padrão brasileiro."""
    if valor is None or pd.isna(valor):
        return "N/D"
    sinal = "+" if valor > 0 else ""
    return f"{sinal}{br(valor, casas)}"


def fmt_date(d):
    if d is None or pd.isna(d):
        return "N/D"
    return pd.to_datetime(d).strftime("%d/%m/%Y")


# ------------------------------------------------------------ abertura
st.title("Impacto da Chuva na Velocidade dos Ônibus (SPTrans)")

db_path = queries.get_db_path()
if not db_path.exists():
    try:
        with st.spinner("Baixando base analítica Gold (primeira execução)..."):
            queries.ensure_gold_db()
    except Exception as exc:
        st.error(
            f"Banco não encontrado em `{db_path}` e não foi possível baixar da Release.\n\n"
            f"Detalhe: `{exc}`\n\n"
            "Localmente, rode `python src/pipeline.py` antes de abrir o painel."
        )
        st.stop()

# ------------------------------------------------------------- filtros
with st.sidebar:
    st.header("Filtros")
    options = queries.get_filter_options(min_readings_for_default=MIN_READINGS)

    selected_routes = st.multiselect(
        "Linhas de ônibus",
        options=options["routes"],
        default=options["default_routes"],
        help="Selecione as linhas de ônibus para análise.",
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

st.caption(
    f"Período analisado: **{fmt_date(options['min_date'])} a {fmt_date(options['max_date'])}** "
    f"({options['days_count']} dias) · Telemetria GPS SPTrans + Pluviômetros CEMADEN."
)

with st.expander("Como ler este painel"):
    st.markdown(
        "- **Condição meteorológica:** compara leituras **sem chuva (`0 mm/h`)** contra leituras **com chuva (`> 0 mm/h`)** na estação CEMADEN mais próxima.\n"
        "- **Impacto da Chuva (Delta):** diferença entre a velocidade na chuva e a média sem chuva "
        "**da mesma linha, mesmo sentido, mesmo trecho (estação), mesmo tipo de dia e mesma hora** — evitando distorções entre linhas mais rápidas e mais lentas.\n"
        "- **Cores:** vermelho indica **perda de velocidade** (ônibus mais lento na chuva); azul indica ganho."
    )

# ------------------------------------------------------------- cartoes
kpis = queries.get_overview_metrics(
    route_ids=selected_routes,
    peak_mode=peak_choice,
    include_unknown=False,
)
route_df = queries.get_speed_by_route(
    route_ids=selected_routes,
    peak_mode=peak_choice,
    include_unknown=False,
    min_readings=MIN_READINGS,
)
comparable_routes = len(route_df)
affected_routes = (
    int((route_df["avg_diff_from_dry_kmh"] < 0).sum()) if comparable_routes > 0 else 0
)

c1, c2, c3, c4, c5 = st.columns(5)
c1.metric(
    "Seco (mesmo trecho/horário)",
    f"{br(kpis['dry_avg_speed_kmh'], 2)} km/h" if kpis["dry_avg_speed_kmh"] is not None else "N/D",
    delta=f"base: {br(kpis['dry_readings'], 0)} leituras secas",
    delta_color="off",
    help="Média sem chuva nos mesmos trechos, sentidos, tipo de dia e horários em que choveu.",
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
    "Linhas com Redução",
    f"{affected_routes} de {comparable_routes}",
    delta=(
        f"{round((affected_routes / comparable_routes) * 100)}% das comparáveis"
        if comparable_routes > 0
        else None
    ),
    delta_color="off",
)
c5.metric(
    "Amostra Analisada",
    br(kpis["total_ok_readings"], 0),
    delta=f"{kpis['rain_routes_count']} linha(s) com chuva",
    delta_color="off",
)

st.divider()

# ---------------------------------------------------------------- abas
tab_geral, tab_linhas, tab_dia = st.tabs(
    ["Visão Geral", "Por Linha", "Por Dia"]
)

# ====================================================== ABA 1: VISÃO GERAL
with tab_geral:
    hourly_title = (
        "Perfil Horário (Pico): Velocidade Média no Seco vs. Chuva"
        if peak_choice == "peak"
        else "Perfil Horário (05h–23h): Velocidade Média no Seco vs. Chuva"
    )
    st.subheader(hourly_title)
    hourly_df = queries.get_hourly_profile(
        route_ids=selected_routes,
        peak_mode=peak_choice,
        min_readings=MIN_READINGS,
        hour_start=5,
        hour_end=23,
    )
    if not hourly_df.empty:
        hourly_chart = (
            alt.Chart(hourly_df)
            .mark_line(point=True, strokeWidth=2.5)
            .encode(
                x=alt.X(
                    "hour_of_day:O",
                    title="Hora do dia (SP)",
                    axis=alt.Axis(labelAngle=0),
                ),
                y=alt.Y(
                    "avg_speed_kmh:Q",
                    title="Velocidade média (km/h)",
                    scale=alt.Scale(zero=False),
                ),
                color=alt.Color(
                    "condition:N",
                    title="Condição",
                    scale=alt.Scale(
                        domain=["Seco (mesmo trecho)", "Chuva"],
                        range=["#2E6B9E", "#C0392B"],
                    ),
                ),
                tooltip=[
                    alt.Tooltip("hour_of_day:O", title="Hora"),
                    alt.Tooltip("condition:N", title="Condição"),
                    alt.Tooltip("avg_speed_kmh:Q", title="Velocidade (km/h)", format=".2f"),
                    alt.Tooltip("readings:Q", title="Leituras", format=",.0f"),
                ],
            )
            .properties(height=340)
        )
        st.altair_chart(hourly_chart, use_container_width=True)
    else:
        st.info("Sem amostra suficiente por hora para os filtros atuais.")

    st.info(
        "**Nota sobre intensidade da chuva (fraca / moderada / forte):** a camada Gold classifica cada leitura "
        "por faixa de precipitação, mas este painel compara **Seco vs. Chuva** no agregado. Na janela coletada "
        f"({options['days_count']} dias), a grande maioria das ocorrências foi de chuva fraca (≤ 2,5 mm/h), "
        "com poucos episódios independentes de chuva moderada e forte — volume ainda insuficiente para "
        "conclusões estatísticas robustas por faixa de intensidade."
    )

# ====================================================== ABA 2: LINHAS E MAPA
with tab_linhas:
    st.subheader("Impacto por Linha (maior perda → menor)")
    if not route_df.empty:
        route_bars = (
            alt.Chart(route_df)
            .mark_bar()
            .encode(
                y=alt.Y(
                    "route_id:N",
                    sort=alt.SortField(field="avg_diff_from_dry_kmh", order="ascending"),
                    title="Linha",
                ),
                x=alt.X("avg_diff_from_dry_kmh:Q", title="Delta vs. Seco (km/h)"),
                color=alt.condition(
                    alt.datum.avg_diff_from_dry_kmh < 0,
                    alt.value(COLOR_LOSS),
                    alt.value(COLOR_GAIN),
                ),
                tooltip=[
                    alt.Tooltip("route_id:N", title="Linha"),
                    alt.Tooltip("route_name:N", title="Nome"),
                    alt.Tooltip("dry_avg_speed_kmh:Q", title="Seco (km/h)", format=".2f"),
                    alt.Tooltip("rain_avg_speed_kmh:Q", title="Chuva (km/h)", format=".2f"),
                    alt.Tooltip("avg_diff_from_dry_kmh:Q", title="Impacto (km/h)", format="+.2f"),
                    alt.Tooltip("avg_diff_pct:Q", title="Impacto (%)", format="+.1f"),
                    alt.Tooltip("dry_readings:Q", title="Leituras (Seco)", format=",.0f"),
                    alt.Tooltip("rain_readings:Q", title="Leituras (Chuva)", format=",.0f"),
                    alt.Tooltip("readings:Q", title="Total Válidas", format=",.0f"),
                ],
            )
        )
        zero_x = alt.Chart(pd.DataFrame({"x": [0]})).mark_rule(
            color="#666666", strokeDash=[4, 4]
        ).encode(x="x:Q")
        st.altair_chart(
            (route_bars + zero_x).properties(height=max(280, len(route_df) * 42)),
            use_container_width=True,
        )
    else:
        st.info(f"Nenhuma linha atingiu o mínimo de {MIN_READINGS} leituras no seco e na chuva.")

# ====================================================== ABA 3: POR DIA
with tab_dia:
    st.subheader("Resumo por Dia da Coleta")
    daily_df = queries.get_daily_summary(
        route_ids=selected_routes,
        peak_mode=peak_choice,
        min_rain_readings=100,
    )
    if not daily_df.empty:
        st.dataframe(
            daily_df,
            hide_index=True,
            use_container_width=True,
            column_config={
                "date_sp": st.column_config.DateColumn("Data", format="DD/MM/YYYY"),
                "dry_readings": st.column_config.NumberColumn("Leituras (Seco)", format="%d"),
                "rain_readings": st.column_config.NumberColumn("Leituras (Chuva)", format="%d"),
                "max_precipitation_mm": st.column_config.NumberColumn(
                    "Pico de Chuva (mm/h)", format="%.2f"
                ),
                "avg_diff_from_dry_kmh": st.column_config.NumberColumn(
                    "Impacto Médio (km/h)", format="%.2f"
                ),
            },
        )
    else:
        st.info("Sem dados disponíveis para os filtros selecionados.")

# ---------------------------------------------------- rodape de proveniencia
st.divider()
prov = queries.get_provenance_summary()
if prov:
    st.caption(
        f"Fonte: API SPTrans Olho Vivo & CEMADEN | Etapa: `{prov.get('stage')}` | "
        f"Registros na Gold: {br(prov.get('records'), 0)} | Atualizado em: {prov.get('executed_at')}"
    )
else:
    st.caption("Fonte: API SPTrans Olho Vivo & CEMADEN (data/gold/project.duckdb).")
