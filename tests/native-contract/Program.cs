using System.Text.Json;
using SaltPlayer.Native.Models;

var cases = new List<(string Name, Action Run)>();
void Test(string name, Action run) => cases.Add((name, run));
void Check(bool value, string reason) { if (!value) throw new InvalidOperationException(reason); }
NativeState State(string error) => JsonSerializer.Deserialize<NativeState>(
    "{\"ok\":true,\"playing\":false,\"ended\":true,\"error\":" + error + "}")!;
void Reject(string error)
{
    try { State(error); }
    catch (JsonException) { return; }
    throw new InvalidOperationException("unsupported error shape was accepted");
}

Test("native error object preserves actual detail", () =>
{
    var state = State("{\"kind\":\"decode_failed\",\"detail\":\"synthetic corrupt frame\",\"rc\":1,\"frames\":0}");
    Check(state.Error?.Contains("synthetic corrupt frame") == true, "object detail missing");
    Check(state.Ended && !state.Playing, "converter consumed neighboring state fields");
});
Test("legacy string error remains compatible", () => Check(State("\"legacy decoder failure\"").Error == "legacy decoder failure", "string drift"));
Test("null error remains null", () => Check(State("null").Error is null, "null drift"));
Test("missing error remains null", () => Check(JsonSerializer.Deserialize<NativeState>("{\"ok\":true}")!.Error is null, "missing error drift"));
Test("empty error object cannot turn into success", () => Check(!string.IsNullOrWhiteSpace(State("{}").Error), "object must remain a failure"));
Test("known error kind without detail has useful fallback", () => Check(State("{\"kind\":\"decoder_unavailable\"}").Error?.Contains("解码") == true, "known kind fallback"));
Test("unknown error kind remains visible failure", () => Check(!string.IsNullOrWhiteSpace(State("{\"kind\":\"new_kind\",\"extra\":{\"n\":1}}").Error), "unknown error disappeared"));
foreach (var bad in new[] { "42", "true", "[]", "[{}]", "{\"detail\":[]}", "{\"kind\":42}" })
{
    var input = bad;
    Test("reject unsupported error shape " + input, () => Reject(input));
}
Test("error detail has a bounded display length", () =>
{
    var state = State(JsonSerializer.Serialize(new { kind = "decode_failed", detail = new string('x', 5000) }));
    Check(state.Error!.Length <= 1024, "unbounded error display");
});
Test("duplicate error detail is rejected instead of ambiguous interpretation", () => Reject("{\"detail\":\"first\",\"detail\":\"second\"}"));

object Decision(NativeState state)
{
    var type = typeof(NativeState).Assembly.GetType("SaltPlayer.Native.Models.NativePlaybackDecision");
    Check(type is not null, "production NativePlaybackDecision is missing");
    return type!.GetMethod("From")!.Invoke(null, new object[] { state })!;
}
T Field<T>(object value, string field) => (T)value.GetType().GetProperty(field)!.GetValue(value)!;
Test("decode error is a stopped failure, never natural completion", () =>
{
    var d = Decision(State("{\"kind\":\"decode_failed\",\"detail\":\"bad audio\"}"));
    Check(Field<string?>(d, "Error")?.Contains("bad audio") == true, "error not visible");
    Check(!Field<bool>(d, "IsPlaying") && !Field<bool>(d, "NaturalEnd"), "failure triggered transport/next");
});
Test("legacy failure stops even if backend says playing", () =>
{
    var d = Decision(new NativeState { Ok = true, Playing = true, Ended = false, Error = "failed" });
    Check(!Field<bool>(d, "IsPlaying") && Field<string?>(d, "Error") == "failed", "legacy failure ignored");
});
Test("not-ok response has an actionable fallback", () =>
{
    var d = Decision(new NativeState { Ok = false, Ended = true, NativeNote = "device unavailable" });
    Check(Field<string?>(d, "Error") == "device unavailable" && !Field<bool>(d, "NaturalEnd"), "not-ok advanced queue");
});
Test("clean ended state is natural completion", () =>
{
    var d = Decision(new NativeState { Ok = true, Ended = true });
    Check(Field<bool>(d, "NaturalEnd") && !Field<bool>(d, "IsPlaying") && Field<string?>(d, "Error") is null, "natural end lost");
});
Test("retry after error can play without sticky failure", () =>
{
    Decision(State("\"failed\""));
    var d = Decision(new NativeState { Ok = true, Playing = true, Duration = 100, Position = 2 });
    Check(Field<bool>(d, "IsPlaying") && !Field<bool>(d, "NaturalEnd") && Field<string?>(d, "Error") is null, "sticky failure");
});
Test("paused state does not imply end", () =>
{
    var d = Decision(new NativeState { Ok = true, Playing = false });
    Check(!Field<bool>(d, "IsPlaying") && !Field<bool>(d, "NaturalEnd"), "pause advanced queue");
});
foreach (var bad in new[] { double.NaN, double.PositiveInfinity, -1.0 })
{
    var value = bad;
    Test("invalid position is not rendered or played " + value, () =>
    {
        var d = Decision(new NativeState { Ok = true, Playing = true, Position = value });
        Check(Field<string?>(d, "Error") is not null && !Field<bool>(d, "IsPlaying"), "invalid position escaped");
    });
}

const string GoodState = "{\"ok\":true,\"playing\":true,\"position\":2,\"duration\":100,\"ended\":false,\"error\":null}";
const string ErrorState = "{\"ok\":true,\"playing\":false,\"position\":2,\"duration\":100,\"ended\":true,\"error\":{\"kind\":\"decode_failed\",\"detail\":\"broken input\"}}";
(SaltPlayer.Native.Services.PlayerService Player, SaltPlayer.Native.Services.ApiClient Api) Service()
{
    var api = new SaltPlayer.Native.Services.ApiClient();
    api.Handler = (_, path, _) => Task.FromResult(path.StartsWith("native") ? GoodState : "{\"ok\":true}");
    return (new(api, new Microsoft.UI.Dispatching.DispatcherQueue()), api);
}
Task Poll(SaltPlayer.Native.Services.PlayerService player) => (Task)player.GetType()
    .GetMethod("PollAsync", System.Reflection.BindingFlags.Instance | System.Reflection.BindingFlags.NonPublic)!.Invoke(player, null)!;
Test("real PlayerService refuses failed play response", () =>
{
    var (player, api) = Service();
    api.Handler = (_, _, _) => Task.FromResult(ErrorState);
    var errors = new List<string>(); player.ErrorOccurred += errors.Add;
    player.PlayFromAsync(new[] { new Track { Id = 1, Duration = 100 } }, 0, "fixture").GetAwaiter().GetResult();
    Check(!player.IsPlaying && !player.IsBusy, "failed start still marked playing");
    Check(errors.Count == 1 && player.LastError?.Contains("broken input") == true, "actual failure was not displayed");
    Check(!player.Current!.IsPlaying, "track flag remained playing");
});
Test("real polling decoder failure never advances queue and does not become offline", () =>
{
    var (player, api) = Service();
    player.PlayFromAsync(new[] { new Track { Id=1, Duration=100 }, new Track { Id=2, Duration=100 } }, 0, "fixture").GetAwaiter().GetResult();
    var errors = new List<string>(); player.ErrorOccurred += errors.Add;
    api.Handler = (_, path, _) => Task.FromResult(path == "native/state" ? ErrorState : GoodState);
    for (int i=0;i<8;i++) Poll(player).GetAwaiter().GetResult();
    Check(player.Current?.Id == 1 && player.Index == 0, "decoder failure advanced queue");
    Check(!player.IsPlaying && errors.Count == 1 && errors[0].Contains("broken input"), "decode failure missing or spammed");
    Check(!errors.Any(x => x.Contains("无响应")), "decode failure misclassified offline");
});
Test("real natural end still advances to the next track", () =>
{
    var (player, api) = Service();
    player.PlayFromAsync(new[] { new Track { Id=1, Duration=100 }, new Track { Id=2, Duration=100 } }, 0, "fixture").GetAwaiter().GetResult();
    api.Handler = (_, path, _) => Task.FromResult(path == "native/state"
        ? "{\"ok\":true,\"playing\":false,\"ended\":true,\"position\":100,\"duration\":100}" : GoodState);
    Poll(player).GetAwaiter().GetResult();
    Check(player.Current?.Id == 2 && player.IsPlaying, "natural completion no longer advances");
});
Test("late successful play response cannot overwrite a newer failure", () =>
{
    var (player, api) = Service();
    var old = new TaskCompletionSource<string>();
    api.Handler = (_, path, payload) => path == "native" && payload.Contains("\"track_id\":1")
        ? old.Task : Task.FromResult(path == "native" ? ErrorState : "{\"ok\":true}");
    var first = player.PlayFromAsync(new[] { new Track { Id=1, Duration=100 } }, 0, "old");
    player.PlayFromAsync(new[] { new Track { Id=2, Duration=100 } }, 0, "new").GetAwaiter().GetResult();
    old.SetResult(GoodState); first.GetAwaiter().GetResult();
    Check(player.Current?.Id == 2 && !player.IsPlaying && player.LastError?.Contains("broken input") == true, "stale reply changed current state");
});

Test("real error retry rearms natural-end advancement", () =>
{
    var (player, api) = Service();
    player.PlayFromAsync(new[] { new Track { Id=1, Duration=100 }, new Track { Id=2, Duration=100 } }, 0, "fixture").GetAwaiter().GetResult();
    api.Handler = (_, path, _) => Task.FromResult(path == "native/state" ? ErrorState : GoodState);
    Poll(player).GetAwaiter().GetResult();
    api.Handler = (_, _, _) => Task.FromResult(GoodState);
    player.ToggleAsync().GetAwaiter().GetResult();
    Check(player.IsPlaying && player.LastError is null, "retry kept error or paused state");
    api.Handler = (_, path, _) => Task.FromResult(path == "native/state"
        ? "{\"ok\":true,\"playing\":false,\"ended\":true,\"duration\":100,\"position\":100}" : GoodState);
    Poll(player).GetAwaiter().GetResult();
    Check(player.Current?.Id == 2, "retry never rearmed auto-next");
});
Test("removing pending current track cancels busy generation", () =>
{
    var (player, api) = Service();
    var old = new TaskCompletionSource<string>();
    api.Handler = (_, path, payload) => path == "native" && payload.Contains("\"track_id\":1")
        ? old.Task : Task.FromResult(path == "native" ? GoodState : "{\"ok\":true}");
    var first = player.PlayFromAsync(new[] { new Track {Id=1,Duration=100},new Track{Id=2,Duration=100} },0,"fixture");
    player.RemoveAtAsync(0).GetAwaiter().GetResult();
    old.SetResult(GoodState); first.GetAwaiter().GetResult();
    Check(player.Current?.Id == 2 && !player.IsBusy && !player.IsPlaying, "pending removal left permanent busy");
    player.ToggleAsync().GetAwaiter().GetResult();
    Check(player.IsPlaying, "remaining track is not playable");
});

string? ProtocolError(string json)
{
    var type = typeof(NativeState).Assembly.GetType("SaltPlayer.Native.Models.ApiProtocol");
    Check(type is not null, "production ApiProtocol missing");
    var value = JsonSerializer.Deserialize(json, type!, new JsonSerializerOptions
    { NumberHandling = System.Text.Json.Serialization.JsonNumberHandling.AllowReadingFromString });
    return value is null ? null : (string?)type!.GetMethod("GetCompatibilityError")!.Invoke(value, null);
}
foreach (var json in new[] { "null", "{\"major\":1,\"minor\":0,\"capabilities\":[\"native-error-object-v1\"]}",
    "{\"major\":1,\"minor\":99,\"capabilities\":[\"unknown-addition\"],\"future_field\":true}" })
{
    var input = json;
    Test("compatible protocol " + input, () => Check(ProtocolError(input) is null, "valid or legacy protocol rejected"));
}
foreach (var json in new[] { "{}", "{\"major\":2,\"minor\":0,\"capabilities\":[]}",
    "{\"major\":0,\"minor\":0,\"capabilities\":[]}", "{\"major\":1,\"minor\":-1,\"capabilities\":[]}",
    "{\"major\":\"1\",\"minor\":0,\"capabilities\":[]}", "{\"major\":1,\"minor\":0,\"capabilities\":null}",
    "{\"major\":1,\"minor\":0,\"capabilities\":[null]}", "[]" })
{
    var input = json;
    Test("incompatible or malformed protocol " + input, () =>
    {
        try { Check(ProtocolError(input) is not null, "malformed protocol accepted"); }
        catch (JsonException) { }
    });
}

object PresentationCall(string method, params object?[] args)
{
    var type = typeof(NativeState).Assembly.GetType("SaltPlayer.Native.Models.PlayerPresentation");
    Check(type is not null, "production PlayerPresentation missing");
    var target = type!.GetMethod(method);
    Check(target is not null, "production presentation method missing: " + method);
    return target!.Invoke(null, args)!;
}
Test("empty library duration is zero, not one minute", () => Check((string)PresentationCall("FormatDuration", 0.0) == "0 分钟", "invented empty duration"));
Test("subminute library duration is clearly labeled", () => Check((string)PresentationCall("FormatDuration", 20.0) == "不足 1 分钟", "subminute rounded into whole minute"));
Test("nonfinite library duration is safely displayed", () => Check((string)PresentationCall("FormatDuration", double.PositiveInfinity) == "0 分钟", "invalid duration"));
Test("empty hero action adds a library instead of playing nothing", () =>
{
    var hero = PresentationCall("HomeAction", null, null, false);
    Check(Field<string>(hero,"Label") == "添加音乐文件夹" && Field<string>(hero,"Action") == "add-library", "empty action is not actionable");
});
Test("recommended hero keeps displayed and clicked track identity", () =>
{
    var hero = PresentationCall("HomeAction", null, (long?)42, false);
    Check(Field<long?>(hero,"TrackId") == 42 && Field<string>(hero,"Action") == "play-recommended", "wrong recommended track");
});
Test("current hero toggles actual transport state", () =>
{
    var hero = PresentationCall("HomeAction", (long?)7, (long?)42, true);
    Check(Field<long?>(hero,"TrackId") == 7 && Field<string>(hero,"Label") == "暂停", "current action drift");
});

foreach (var key in new[] { "Delete", "Up", "Down", "Left", "Right", "Space", "N" })
{
    var input = key;
    Test("global shortcuts yield to focused controls " + input, () =>
        Check(!(bool)PresentationCall("AllowsGlobalShortcut", input, false, true, false), "stole control keyboard input"));
}
Test("Delete never clears queue globally", () => Check(!(bool)PresentationCall("AllowsGlobalShortcut","Delete",false,false,false), "destructive global shortcut"));
Test("unfocused transport keyboard remains available", () => Check((bool)PresentationCall("AllowsGlobalShortcut","Space",false,false,false), "transport shortcut lost"));
Test("typing and modified shortcuts are not intercepted", () =>
{
    Check(!(bool)PresentationCall("AllowsGlobalShortcut","N",true,false,false), "typing intercepted");
    Check(!(bool)PresentationCall("AllowsGlobalShortcut","N",false,false,true), "modified key intercepted");
});

int failed = 0;
foreach (var (name, run) in cases)
{
    try { run(); Console.WriteLine("PASS " + name); }
    catch (Exception ex) { failed++; Console.WriteLine("FAIL " + name + " :: " + ex.GetType().Name + ": " + ex.Message); }
}
Console.WriteLine(JsonSerializer.Serialize(new { tests = cases.Count, passed = cases.Count - failed, failed }));
return failed == 0 ? 0 : 1;
