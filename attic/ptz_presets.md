# Parked: pan/tilt presets

Removed from the UI because presets didn't work on the W101-style camera
(model 006, firmware 03.03.92): `get_all_pos` answers `-5&value=0,0,0,0,0`
and saving/going to a preset had no visible effect.

The backend is still in place, so only the UI needs restoring:

- `kodak.ptz_command()` builds `preset_go` / `preset_save` / `preset_remove`
  (`go_pos&pos_id=N&moving_mode=0`, `set_pos&pos_id=N&stop_time=500`,
  `remove_pos&pos_id=N`), sent over the VLVL session channel.
- `kodak.get_presets()` reads `get_all_pos`; `get_status()` returns it as
  `presets` (None when the camera reports an error).

Ideas if picking this back up: presets may need `motor_calibration`
first, or presets may only exist on firmware ≥ 03.02.20 with a different
`moving_mode`; capture what `go_pos` replies over the session channel.

## UI that was removed

index.html (inside the pan/tilt card):

```html
<p class="muted small">Hold a direction to move. Presets:</p>
<div id="ptz-presets" class="presets"></div>
```

app.js — in `renderStatus(s)`:

```js
if (s.pan_tilt) {
  const presets = s.presets || Array(5).fill(0);
  $("#ptz-presets").innerHTML = presets.map((v, i) => `
    <div class="preset-row"><span>${i + 1}</span>
    <button class="btn" data-preset="go" data-slot="${i + 1}" ${v ? "" : "disabled"}>Go</button>
    <button class="btn" data-preset="save" data-slot="${i + 1}">Save</button>
    <button class="btn ghost" data-preset="remove" data-slot="${i + 1}" ${v ? "" : "disabled"} aria-label="Delete preset ${i + 1}">✕</button></div>`).join("");
}
```

app.js — in `bindPtz()`:

```js
$("#ptz-presets").addEventListener("click", async (e) => {
  const b = e.target.closest("[data-preset]");
  if (!b) return;
  const slot = Number(b.dataset.slot);
  if (b.dataset.preset === "remove" && !confirm(`Delete preset ${slot}?`)) return;
  try {
    await camAction({ action: `preset_${b.dataset.preset}`, slot });
    toast({ go: "Moving to preset", save: "Preset saved", remove: "Preset deleted" }[b.dataset.preset]);
    if (b.dataset.preset !== "go") refreshStatus();
  } catch (err) { toast(err.message, true); }
});
```

style.css:

```css
.presets { display: flex; flex-direction: column; gap: 6px; margin-bottom: 12px; }
.preset-row { display: grid; grid-template-columns: 22px 1fr 1fr auto; gap: 6px; align-items: center; font-size: 14px; }
.preset-row > span { color: var(--muted); }
```
