import pandas as pd
import geopandas as gpd
from shapely.geometry import LineString
import os

def load_csv(file_path, usecols):
    return pd.read_csv(file_path, usecols=usecols, dtype=str)

def get_unique_trips(trips_df):
    return trips_df.drop_duplicates(subset=['shape_id'])

def merge_transit_data(shapes, trips, routes):
    unique_trips = get_unique_trips(trips)
    shapes_trips = pd.merge(shapes, unique_trips, on='shape_id', how='left')
    final_dataset = pd.merge(shapes_trips, routes, on='route_id', how='inner')
    
    final_dataset['shape_pt_lat'] = pd.to_numeric(final_dataset['shape_pt_lat'])
    final_dataset['shape_pt_lon'] = pd.to_numeric(final_dataset['shape_pt_lon'])
    final_dataset['shape_pt_sequence'] = pd.to_numeric(final_dataset['shape_pt_sequence'])
    
    return final_dataset

def create_geometries(df):
    df = df.sort_values(['shape_id', 'shape_pt_sequence'])
    
    group_cols = ['shape_id', 'route_id', 'route_short_name', 'route_long_name', 'direction_id', 'trip_headsign']
    
    lines = df.groupby(group_cols).apply(
        lambda x: LineString(zip(x['shape_pt_lon'], x['shape_pt_lat'])) if len(x) > 1 else None
    ).reset_index(name='geometry')
    
    lines = lines.dropna(subset=['geometry'])
    gdf = gpd.GeoDataFrame(lines, geometry='geometry', crs="EPSG:4326")
    
    return gdf

def build_gtfs_dataset(gtfs_dir, output_dir=None):
    shapes_cols = ['shape_id', 'shape_pt_lat', 'shape_pt_lon', 'shape_pt_sequence']
    trips_cols = ['route_id', 'shape_id', 'direction_id', 'trip_headsign']
    routes_cols = ['route_id', 'route_short_name', 'route_long_name', 'route_type']

    shapes_df = load_csv(os.path.join(gtfs_dir, 'shapes.csv'), shapes_cols)
    trips_df = load_csv(os.path.join(gtfs_dir, 'trips.csv'), trips_cols)
    routes_df = load_csv(os.path.join(gtfs_dir, 'routes.csv'), routes_cols)

    # Filter only buses and drop the column
    routes_df = routes_df[routes_df['route_type'] == '3'].copy()
    routes_df = routes_df.drop(columns=['route_type'])

    dataset = merge_transit_data(shapes_df, trips_df, routes_df)
    gdf = create_geometries(dataset)

    if output_dir:
        output_path = os.path.join(output_dir, 'routes_geometry.geojson')
        gdf.to_file(output_path, driver="GeoJSON")
        print(f"Dataset saved successfully at: {output_path}")

    return gdf