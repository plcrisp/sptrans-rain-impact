-- Passes when zero duplicate composite grain keys exist
select
    vehicle_id,
    route_code,
    observed_at,
    count(*) as row_count
from {{ ref('fact_bus_readings') }}
group by vehicle_id, route_code, observed_at
having count(*) > 1
