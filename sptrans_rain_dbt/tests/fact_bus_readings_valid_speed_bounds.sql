-- Passes when all readings flagged as 'ok' have valid speed within [0, 80] km/h
select
    vehicle_id,
    route_code,
    observed_at,
    speed_kmh,
    speed_flag
from {{ ref('fact_bus_readings') }}
where speed_flag = 'ok'
  and (speed_kmh is null or speed_kmh < 0 or speed_kmh > 80)
