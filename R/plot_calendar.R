library(tidyverse)
library(glue)
source("R/db_connect.R")

gtfs_schema <- "gtfs_20260921"

calendar_dates <- as_tibble(DBI::dbGetQuery(con, glue(
"
SELECT DISTINCT cd.service_id, cd.date, cd.exception_type, a.agency_name
FROM {gtfs_schema}.trip_patterns tp
CROSS JOIN LATERAL unnest(tp.trip_ids) AS u(trip_id)
JOIN {gtfs_schema}.rail_trips t ON t.trip_id = u.trip_id
JOIN {gtfs_schema}.routes r ON r.route_id = t.route_id
JOIN {gtfs_schema}.agency a ON a.agency_id = r.agency_id
JOIN {gtfs_schema}.calendar_dates cd ON cd.service_id = t.service_id;
")))

chunks <- calendar_dates %>%
  arrange(as.integer(service_id)) |> 
  distinct(service_id) %>%
  mutate(group_id = ntile(row_number(), 10))

calendar_dates |> 
  mutate(
    date = as_date(date),
    order = as.integer(service_id)
    ) |> 
  left_join(chunks, by = "service_id") |> 
  ggplot() +
  geom_point(aes(date, reorder(service_id, order), col = exception_type)) +
  scale_color_manual(
    values = c("1" = "darkgreen", "2" = "red"),
    name = "Exception type"
  ) +
  labs(y = "Service_id") +
  facet_wrap(vars(group_id), scales = "free_y", ncol = 10) 
  
ggsave(glue("output/calendar_expections_{gtfs_schema}.png"), unit = "cm", width = 50, height = 50, dpi = 300)

agency_trips <- as_tibble(DBI::dbGetQuery(con, glue(
"
SELECT pd.date, a.agency_name, sum(pd.n_trips) AS n_trips
FROM {gtfs_schema}.pattern_days pd
JOIN {gtfs_schema}.trip_patterns tp USING (pattern_id)
JOIN {gtfs_schema}.routes r ON r.route_id = tp.route_id
JOIN {gtfs_schema}.agency a ON a.agency_id = r.agency_id
GROUP BY 1, 2
ORDER BY 2, 1;
")))

agency_trips |> 
  ggplot() +
  geom_area(aes(date, n_trips)) +
  labs(y = "Number of trips") +
  facet_grid(vars(agency_name))

ggsave(glue("output/agency_trips_{gtfs_schema}.png"), unit = "cm", width = 20, height = 40, dpi = 300) 
