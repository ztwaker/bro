@load metrics/base
@load http

redef enum Metrics::ID += {
	HTTP_REQUESTS_BY_STATUS_CODE,
	HTTP_REQUESTS_BY_HOST,
};

event bro_init()
	{
	Metrics::create(HTTP_REQUESTS_BY_STATUS_CODE, [$aggregation_mask=24, $break_interval=10secs]);
	Metrics::create(HTTP_REQUESTS_BY_HOST, [$break_interval=10secs]);
	}

event HTTP::log_http(rec: HTTP::Info)
	{
	if ( rec?$host )
		Metrics::add_data(HTTP_REQUESTS_BY_HOST, [$index=rec$host]);
	if ( rec?$status_code )
		Metrics::add_data(HTTP_REQUESTS_BY_STATUS_CODE, [$host=rec$id$orig_h, $index=fmt("%d", rec$status_code)]);
	}