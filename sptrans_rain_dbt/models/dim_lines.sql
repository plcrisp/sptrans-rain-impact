select distinct
    route_code,
    route_id,
    route_name,
    direction_id,
    service_direction_code
from {{ source('silver', 'selected_lines') }}
