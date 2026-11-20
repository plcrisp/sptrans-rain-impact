select distinct
    hour_sp as hour_key,
    cast(hour_sp as date) as date_sp,
    extract('hour' from hour_sp)::integer as hour_of_day,
    extract('isodow' from hour_sp)::integer as day_of_week,
    (extract('isodow' from hour_sp) >= 6)::boolean as is_weekend,
    (
        extract('isodow' from hour_sp) <= 5
        and (
            (extract('hour' from hour_sp) >= 6 and extract('hour' from hour_sp) < 9)
            or (extract('hour' from hour_sp) >= 17 and extract('hour' from hour_sp) < 20)
        )
    )::boolean as is_peak
from {{ source('silver', 'sptrans_rain_enriched') }}
where hour_sp is not null
