-- 数据库明确失败后的补偿脚本；仅释放仍属于本请求的 Redis 占位。
-- KEYS: 库存、批次时段、用户额度、用户时段集合、请求结果。
-- 统一读取 Redis 键类型，避免把类型错误的键当作预约状态处理。
local function key_type(key)
    local result = redis.call("TYPE", key)
    if type(result) == "table" then
        return result["ok"]
    end
    return result
end

local request_id = ARGV[1]
local user_id = ARGV[2]
local ttl_seconds = tonumber(ARGV[3])
local error_code = ARGV[4]
local compensated_at = ARGV[5]
local slot_count = tonumber(ARGV[6])

local expected_types = {"hash", "hash", "string", "set", "hash"}
for index = 1, #KEYS do
    local actual_type = key_type(KEYS[index])
    if actual_type ~= "none" and actual_type ~= expected_types[index] then
        return {"REDIS_STATE_INVALID", "0", "0", "0"}
    end
end
if not ttl_seconds or ttl_seconds < 1 or not slot_count or #ARGV ~= 6 + slot_count then
    return {"REDIS_STATE_INVALID", "0", "0", "0"}
end
if redis.call("HGET", KEYS[5], "request_id") ~= request_id
    or redis.call("HGET", KEYS[5], "user_id") ~= user_id then
    return {"OWNERSHIP_MISMATCH", "0", "0", redis.call("GET", KEYS[3]) or "0"}
end
if redis.call("HGET", KEYS[5], "compensation_status") == "RELEASED" then
    -- 重复补偿保持幂等，不再次减少额度。
    return {"RELEASED", "1", "0", redis.call("GET", KEYS[3]) or "0"}
end

local expected_owner = "RESERVED:" .. request_id .. ":" .. user_id
-- 逐时段检查占位所有者，防止释放后被别人重新占用的库存。
local released_count = 0
for index = 1, slot_count do
    local slot_id = ARGV[6 + index]
    if redis.call("HGET", KEYS[1], slot_id) == expected_owner then
        local period_token = redis.call("HGET", KEYS[2], slot_id)
        redis.call("HSET", KEYS[1], slot_id, "AVAILABLE")
        if period_token then
            redis.call("SREM", KEYS[4], period_token)
        end
        released_count = released_count + 1
    end
end

local current_usage = tonumber(redis.call("GET", KEYS[3]) or "0")
local used_after = math.max(0, current_usage - released_count)
if used_after == 0 then
    redis.call("DEL", KEYS[3])
else
    redis.call("SET", KEYS[3], tostring(used_after), "EX", ttl_seconds)
end
redis.call(
    "HSET",
    KEYS[5],
    "persistence_status", "FAILED",
    "persistence_error_code", error_code,
    "compensation_status", "RELEASED",
    "compensated_at", compensated_at
)
redis.call("EXPIRE", KEYS[5], ttl_seconds)
return {"RELEASED", "0", tostring(released_count), tostring(used_after)}
