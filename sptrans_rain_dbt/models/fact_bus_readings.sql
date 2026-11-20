select
    cast(vehicle_id as varchar) as vehicle_id,
    cast(codigoLinha as integer) as route_code,
    cast(station_id as varchar) as station_id,
    hour_sp as hour_key,
    timestamp_sp as observed_at,
    cast(lat as double) as latitude,
    cast(lon as double) as longitude,
    cast(speed_kmh as double) as speed_kmh,
    cast(speed_flag as varchar) as speed_flag,
    cast(dist_to_station_m as double) as distance_to_station_m,
    cast(rain_mm as double) as precipitation_mm,
    cast(rain_readings_in_hour as integer) as rain_readings_in_hour,
    cast(is_raining as boolean) as is_raining,
    cast(rain_is_missing as boolean) as rain_is_missing,
    cast(rain_intensity as varchar) as rain_intensity,
    cast(dry_baseline_speed_kmh as double) as dry_baseline_speed_kmh,
    cast(speed_diff_from_dry_avg as double) as speed_diff_from_dry_avg
from {{ source('silver', 'sptrans_rain_enriched') }}
where station_id is not null
