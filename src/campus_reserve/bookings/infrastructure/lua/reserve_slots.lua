local function key_type(key)
    local result = redis.call("TYPE", key)
    if type(result) == "table" then
        return result["ok"]
    end
    return result
end

local fingerprint = ARGV[1]
local request_id = ARGV[2]
local user_id = ARGV[3]
local batch_id = ARGV[4]
local service_date = ARGV[5]
local max_daily_slots = tonumber(ARGV[6])
local ttl_seconds = tonumber(ARGV[7])
local slot_count = tonumber(ARGV[8])

local expected_types = {"hash", "hash", "string", "set", "hash", "stream"}
for index = 1, #KEYS do
    local actual_type = key_type(KEYS[index])
    if actual_type ~= "none" and actual_type ~= expected_types[index] then
        return {"REDIS_STATE_INVALID", "0", "0", ""}
    end
end
if not max_daily_slots or max_daily_slots < 1
    or not ttl_seconds or ttl_seconds < 1
    or not slot_count or #ARGV ~= 8 + slot_count then
    return {"REDIS_STATE_INVALID", "0", "0", ""}
end

local current_usage = tonumber(redis.call("GET", KEYS[3]) or "0")
local stored_fingerprint = redis.call("HGET", KEYS[5], "fingerprint")
if stored_fingerprint then
    if stored_fingerprint ~= fingerprint then
        return {"IDEMPOTENCY_CONFLICT", "1", tostring(current_usage), ""}
    end
    return {
        redis.call("HGET", KEYS[5], "code"),
        "1",
        redis.call("HGET", KEYS[5], "used_slot_count") or tostring(current_usage),
        redis.call("HGET", KEYS[5], "stream_id") or ""
    }
end

local function remember_failure(code)
    redis.call(
        "HSET",
        KEYS[5],
        "fingerprint", fingerprint,
        "code", code,
        "used_slot_count", tostring(current_usage),
        "stream_id", "",
        "slot_ids", "",
        "request_id", request_id,
        "user_id", user_id,
        "batch_id", batch_id,
        "service_date", service_date
    )
    redis.call("EXPIRE", KEYS[5], ttl_seconds)
    return {code, "0", tostring(current_usage), ""}
end

if redis.call("HGET", KEYS[2], "__service_date") ~= service_date then
    return remember_failure("BATCH_DATE_MISMATCH")
end
local batch_status = redis.call("HGET", KEYS[2], "__batch_status")
if batch_status and batch_status ~= "OPEN" then
    return remember_failure("BATCH_NOT_OPEN")
end

if slot_count < 1 or slot_count > 2 then
    return remember_failure("INVALID_SLOT_COUNT")
end

local slot_ids = {}
local requested_periods = {}
for index = 1, slot_count do
    local slot_id = ARGV[8 + index]
    if slot_ids[slot_id] then
        return remember_failure("SLOTS_MUST_BE_UNIQUE")
    end
    slot_ids[slot_id] = true

    local slot_state = redis.call("HGET", KEYS[1], slot_id)
    local period_token = redis.call("HGET", KEYS[2], slot_id)
    if not slot_state or not period_token then
        return remember_failure("SLOTS_NOT_FOUND")
    end
    if slot_state ~= "AVAILABLE" then
        return remember_failure("SLOT_NOT_AVAILABLE")
    end
    if requested_periods[period_token]
        or redis.call("SISMEMBER", KEYS[4], period_token) == 1 then
        return remember_failure("TIME_CONFLICT")
    end
    requested_periods[period_token] = true
end

if current_usage + slot_count > max_daily_slots then
    return remember_failure("DAILY_LIMIT_EXCEEDED")
end

local joined_slot_ids = table.concat(ARGV, ",", 9, 8 + slot_count)
local stream_id = redis.call(
    "XADD",
    KEYS[6],
    "*",
    "request_id", request_id,
    "user_id", user_id,
    "batch_id", batch_id,
    "service_date", service_date,
    "slot_ids", joined_slot_ids,
    "fingerprint", fingerprint
)

for index = 1, slot_count do
    local slot_id = ARGV[8 + index]
    local period_token = redis.call("HGET", KEYS[2], slot_id)
    redis.call("HSET", KEYS[1], slot_id, "RESERVED:" .. request_id .. ":" .. user_id)
    redis.call("SADD", KEYS[4], period_token)
end

local used_after = current_usage + slot_count
redis.call("SET", KEYS[3], tostring(used_after), "EX", ttl_seconds)
redis.call("EXPIRE", KEYS[4], ttl_seconds)
redis.call(
    "HSET",
    KEYS[5],
    "fingerprint", fingerprint,
    "code", "RESERVED",
    "used_slot_count", tostring(used_after),
    "stream_id", stream_id,
    "slot_ids", joined_slot_ids,
    "request_id", request_id,
    "user_id", user_id,
    "batch_id", batch_id,
    "service_date", service_date
)
redis.call("EXPIRE", KEYS[5], ttl_seconds)
return {"RESERVED", "0", tostring(used_after), stream_id}
