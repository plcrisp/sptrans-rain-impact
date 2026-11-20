{{ config(materialized='view') }}

select
    f.vehicle_id,
    f.route_code,
    l.route_id,
    l.route_name,
    l.direction_id,
    f.station_id,
    s.station_name,
    s.status as station_status,
    f.hour_key,
    t.date_sp,
    t.hour_of_day,
    t.day_of_week,
    t.is_weekend,
    t.is_peak,
    f.observed_at,
    f.latitude,
    f.longitude,
    f.speed_kmh,
    f.speed_flag,
    f.distance_to_station_m,
    f.precipitation_mm,
    f.rain_readings_in_hour,
    f.is_raining,
    f.rain_is_missing,
    f.rain_intensity,
    f.dry_baseline_speed_kmh,
    f.speed_diff_from_dry_avg
from {{ ref('fact_bus_readings') }} f
join {{ ref('dim_lines') }} l using (route_code)
join {{ ref('dim_stations') }} s using (station_id)
join {{ ref('dim_time') }} t using (hour_key)
