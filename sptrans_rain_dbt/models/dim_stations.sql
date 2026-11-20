select distinct
    station_id,
    station_name,
    city,
    state,
    latitude,
    longitude,
    status,
    station_type
from {{ source('silver', 'stations_catalog') }}
where station_id is not null and station_id <> ''
