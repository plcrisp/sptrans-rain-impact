import html
import os
from typing import Any, Dict, List, Set

import folium
import geopandas as gpd

from src.utils.geo_utils import DEFAULT_MAX_DIST_M
from src.utils.logger import get_logger

logger = get_logger("map_utils")


def generate_map(
    chosen_lines: List[Dict[str, Any]],
    routes_gdf: gpd.GeoDataFrame,
    stations_gdf: gpd.GeoDataFrame,
    stations_needed_ids: Set[str],
    output_html_path: str,
    max_dist_m: float = DEFAULT_MAX_DIST_M,
) -> None:
    """Gera mapa interativo Folium com traçados das linhas e estações ativas/dominantes."""
    if routes_gdf.crs is None:
        routes_gdf = routes_gdf.set_crs(31983)
    if stations_gdf.crs is None:
        stations_gdf = stations_gdf.set_crs(31983)

    # Reprojetar para WGS84 para exibição no Folium
    routes_wgs84 = routes_gdf.to_crs(epsg=4326)
    stations_wgs84 = stations_gdf.to_crs(epsg=4326)

    # Centro de São Paulo
    m = folium.Map(location=[-23.5505, -46.6333], zoom_start=11, tiles="OpenStreetMap")

    # Cores distintas para as linhas selecionadas
    palette = [
        "#E41A1C",  # Vermelho
        "#377EB8",  # Azul
        "#4DAF4A",  # Verde
        "#984EA3",  # Roxo
        "#FF7F00",  # Laranja
        "#A65628",  # Marrom
        "#F781BF",  # Rosa
        "#00C0C7",  # Turquesa
    ]

    dominant_station_ids = {
        line["dominant_station_id"] for line in chosen_lines if line.get("dominant_station_id")
    }

    # 1. Adicionar estações ativas
    for idx, row in stations_wgs84.iterrows():
        st_id = row["station_id"]
        st_name = row["station_name"]
        lat = row.geometry.y
        lon = row.geometry.x

        is_needed = st_id in stations_needed_ids
        is_dominant = st_id in dominant_station_ids

        if is_dominant:
            color = "#D95F02"
            fill_color = "#D95F02"
            radius = 7
            weight = 2
            # Círculo de buffer max_dist_m
            folium.Circle(
                location=[lat, lon],
                radius=max_dist_m,
                color="#D95F02",
                weight=1,
                fill=True,
                fill_color="#D95F02",
                fill_opacity=0.08,
            ).add_to(m)
        elif is_needed:
            color = "#1B9E77"
            fill_color = "#1B9E77"
            radius = 5
            weight = 1
        else:
            color = "#999999"
            fill_color = "#CCCCCC"
            radius = 3
            weight = 1

        popup_text = f"<b>{st_name}</b><br>ID: {st_id}<br>Status: Ativa"
        if is_dominant:
            popup_text += "<br><b>★ Estação Dominante</b>"
        elif is_needed:
            popup_text += "<br><i>Estação Vizinha Coberta</i>"

        folium.CircleMarker(
            location=[lat, lon],
            radius=radius,
            color=color,
            fill=True,
            fill_color=fill_color,
            fill_opacity=0.9,
            weight=weight,
            popup=folium.Popup(popup_text, max_width=300),
        ).add_to(m)

    # 2. Adicionar traçados das linhas escolhidas
    for i, line in enumerate(chosen_lines):
        r_id = line["route_id"]
        color = palette[i % len(palette)]
        line_shapes = routes_wgs84[routes_wgs84["route_id"] == r_id]

        for _, shape_row in line_shapes.iterrows():
            geom = shape_row.geometry
            coords = [(lat, lon) for lon, lat in geom.coords]
            dir_id = shape_row["direction_id"]
            headsign = shape_row["trip_headsign"]

            popup = (
                f"<b>Linha {r_id}</b> ({shape_row['route_long_name']})<br>"
                f"Sentido: {dir_id} ({headsign})<br>"
                f"Extensão: {shape_row['length_km']:.1f} km<br>"
                f"Viagens/dia útil: {shape_row['weekday_trips']}<br>"
                f"Headway Pico: {shape_row['peak_headway_min']:.1f} min"
            )

            folium.PolyLine(
                locations=coords,
                color=color,
                weight=4,
                opacity=0.85,
                popup=folium.Popup(popup, max_width=350),
            ).add_to(m)

    os.makedirs(os.path.dirname(output_html_path), exist_ok=True)
    m.save(output_html_path)
    logger.info(f"Mapa interativo salvo em: {output_html_path}")


def render_map_html(html_path: str, height: int = 600):
    """Renderiza um arquivo HTML do Folium de forma segura e confiável em notebooks Jupyter."""
    from IPython.display import HTML

    if not os.path.exists(html_path):
        raise FileNotFoundError(f"Arquivo de mapa não encontrado em: {html_path}")

    with open(html_path, "r", encoding="utf-8") as f:
        html_content = f.read()

    escaped = html.escape(html_content)
    iframe_html = (
        f'<div style="width: 100%; border: 1px solid #ddd; border-radius: 8px; overflow: hidden;">'
        f'<iframe srcdoc="{escaped}" style="width: 100%; height: {height}px; border: none;" '
        f'allowfullscreen></iframe>'
        f'</div>'
    )
    return HTML(iframe_html)
