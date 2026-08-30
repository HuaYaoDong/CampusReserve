local function key_type(key)
    local result = redis.call("TYPE", key)
    if type(result) == "table" then
        return result["ok"]
    end
    return result
end

local fingerprint = ARGV[1]
local ttl_seconds = tonumber(ARGV[2])
local service_date = ARGV[3]
local slot_count = tonumber(ARGV[4])
local confirmed_user_count = tonumber(ARGV[5])

if not ttl_seconds or ttl_seconds < 1 or service_date == ""
    or not slot_count or slot_count < 1
    or not confirmed_user_count or confirmed_user_count < 0 then
    return {"REDIS_STATE_INVALID", "0", "0"}
end

local slots_type = key_type(KEYS[1])
local periods_type = key_type(KEYS[2])
local marker_type = key_type(KEYS[3])
if (slots_type ~= "none" and slots_type ~= "hash")
    or (periods_type ~= "none" and periods_type ~= "hash")
    or (marker_type ~= "none" and marker_type ~= "string") then
    return {"REDIS_STATE_INVALID", "0", "0"}
end
if #KEYS ~= 3 + confirmed_user_count * 2 then
    return {"REDIS_STATE_INVALID", "0", "0"}
end
for index = 1, confirmed_user_count do
    local usage_key_index = 4 + (index - 1) * 2
    local usage_type = key_type(KEYS[usage_key_index])
    local user_periods_type = key_type(KEYS[usage_key_index + 1])
    if (usage_type ~= "none" and usage_type ~= "string")
        or (user_periods_type ~= "none" and user_periods_type ~= "set") then
        return {"REDIS_STATE_INVALID", "0", "0"}
    end
end

local stored_fingerprint = redis.call("GET", KEYS[3])
if stored_fingerprint then
    if stored_fingerprint == fingerprint then
        return {"INITIALIZED", "1", tostring(slot_count)}
    end
    return {"INVENTORY_CONFLICT", "1", "0"}
end

-- Keys without their marker indicate an interrupted/manual write. Refuse to overwrite them.
for index = 1, #KEYS do
    if redis.call("EXISTS", KEYS[index]) == 1 then
        return {"REDIS_STATE_INVALID", "0", "0"}
    end
end

local seen = {}
for index = 1, slot_count do
    local offset = 6 + (index - 1) * 3
    local slot_id = ARGV[offset]
    local slot_state = ARGV[offset + 1]
    local period_token = ARGV[offset + 2]
    if seen[slot_id]
        or (slot_state ~= "AVAILABLE"
            and slot_state ~= "BOOKED"
            and slot_state ~= "UNAVAILABLE")
        or period_token == "" then
        return {"REDIS_STATE_INVALID", "0", "0"}
    end
    seen[slot_id] = true
end

local confirmed_cursor = 6 + slot_count * 3
local confirmed_usages = {}
for index = 1, confirmed_user_count do
    local user_id = ARGV[confirmed_cursor]
    local used_slot_count = tonumber(ARGV[confirmed_cursor + 1])
    local period_count = tonumber(ARGV[confirmed_cursor + 2])
    if not user_id or user_id == ""
        or not used_slot_count or used_slot_count < 1 or used_slot_count > 2
        or not period_count or period_count ~= used_slot_count then
        return {"REDIS_STATE_INVALID", "0", "0"}
    end
    local periods = {}
    for period_index = 1, period_count do
        local period_token = ARGV[confirmed_cursor + 2 + period_index]
        if not period_token or period_token == "" then
            return {"REDIS_STATE_INVALID", "0", "0"}
        end
        periods[period_index] = period_token
    end
    confirmed_usages[index] = {
        used_slot_count = used_slot_count,
        periods = periods
    }
    confirmed_cursor = confirmed_cursor + 3 + period_count
end
if confirmed_cursor ~= #ARGV + 1 then
    return {"REDIS_STATE_INVALID", "0", "0"}
end

for index = 1, slot_count do
    local offset = 6 + (index - 1) * 3
    redis.call("HSET", KEYS[1], ARGV[offset], ARGV[offset + 1])
    redis.call("HSET", KEYS[2], ARGV[offset], ARGV[offset + 2])
end
for index = 1, confirmed_user_count do
    local usage_key_index = 4 + (index - 1) * 2
    local confirmed = confirmed_usages[index]
    redis.call(
        "SET",
        KEYS[usage_key_index],
        tostring(confirmed.used_slot_count),
        "EX",
        ttl_seconds
    )
    for period_index = 1, #confirmed.periods do
        redis.call("SADD", KEYS[usage_key_index + 1], confirmed.periods[period_index])
    end
    redis.call("EXPIRE", KEYS[usage_key_index + 1], ttl_seconds)
end
redis.call("HSET", KEYS[2], "__service_date", service_date)
redis.call("SET", KEYS[3], fingerprint, "EX", ttl_seconds)
redis.call("EXPIRE", KEYS[1], ttl_seconds)
redis.call("EXPIRE", KEYS[2], ttl_seconds)
return {"INITIALIZED", "0", tostring(slot_count)}
