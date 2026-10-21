"""Временная проверка фактических интерфейсов модулей (не часть пакета)."""
import glob
import inspect
import re

from soundbridge import spotify_client as sc
from soundbridge import yandex_client as yc

print("=== spotify_client ===")
for f in (
    sc.login, sc.SpotifyAccount.connect, sc.SpotifyAccount.me,
    sc.SpotifyAccount.saved_tracks, sc.SpotifyAccount.playlists,
    sc.SpotifyAccount.playlist_tracks, sc.SpotifyAccount.search_track,
    sc.SpotifyAccount.tracks, sc.SpotifyAccount.create_playlist,
    sc.SpotifyAccount.add_tracks, sc.SpotifyAccount.add_tracks_in_batches,
    sc.SpotifyAccount.save_tracks, sc.SpotifyAccount.save_tracks_in_batches,
    sc.SpotifyAccount.paged, sc.SpotifyAccount.request,
):
    print(" ", f.__qualname__, inspect.signature(f))
print("  TokenBundle fields:", getattr(sc.TokenBundle, "__dataclass_fields__", {}).keys())
print("  Quota fields:", getattr(sc.Quota, "__dataclass_fields__", {}).keys())

print("=== yandex_client ===")
for name, obj in vars(yc).items():
    if name.startswith("_"):
        continue
    if inspect.isfunction(obj) and obj.__module__ == "soundbridge.yandex_client":
        print("  def", name, inspect.signature(obj))
    elif inspect.isclass(obj) and obj.__module__ == "soundbridge.yandex_client":
        print("  class", name)
        for m, fn in inspect.getmembers(obj, predicate=inspect.isfunction):
            if not m.startswith("__"):
                print("      ", m, inspect.signature(fn))

print("=== кто как использует клиентов ===")
pat = re.compile(r"\b(sp|client|account|api|yb|acc)\.([a-zA-Z_]+)\(")
for f in sorted(glob.glob("soundbridge/*.py") + glob.glob("scripts/*.py")):
    src = open(f, encoding="utf-8").read()
    used = sorted({"%s.%s()" % m for m in pat.findall(src)})
    print(f, len(src.splitlines()), "lines")
    for u in used:
        print("    ", u)
