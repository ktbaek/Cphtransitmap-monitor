library(tidyverse)
library(glue)
source("R/db_connect.R")

gtfs_schema <- "gtfs_20261019"

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

calendar_dates |> 
  mutate(
    date = as_date(date),
    order = as.integer(service_id)
    ) |> 
  ggplot() +
  geom_point(aes(date, reorder(service_id, order), col = exception_type)) +
  scale_color_manual(
    values = c("1" = "darkgreen", "2" = "red"),
    name = "Exception type"
  ) +
  labs(x = "Date", y = "Service_id") +
  facet_grid(vars(agency_name), scales = "free_y", space = "free_y") +
  scale_x_date(
    date_breaks = "1 week",
    date_minor_breaks = "1 day",
    limits = as_date(calendar_limits)) +
  theme(
    axis.text.y = element_text(size = 4),
    axis.text.x = element_text(size = 18),
    axis.title = element_text(size = 18),
    legend.text = element_text(size = 18),
    legend.title = element_text(size = 18)
  )
  
ggsave(glue("output/calendar_expections_{gtfs_schema}.png"), unit = "cm", width = 50, height = 120, dpi = 300)

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
  geom_area(aes(date, n_trips), alpha = 0.5) +
  geom_line(aes(date, n_trips)) +
  labs(x = "Date", y = "Number of trips") +
  facet_grid(vars(agency_name), scales = "free_y") +
  scale_x_date(
    date_breaks = "1 week",
    date_minor_breaks = "1 day",
    limits = as_date(calendar_limits)) +
  theme(
    axis.text.x = element_text(angle = 45, vjust = 1, hjust = 1)
  )

ggsave(glue("output/agency_trips_{gtfs_schema}.png"), unit = "cm", width = 20, height = 40, dpi = 300) 
