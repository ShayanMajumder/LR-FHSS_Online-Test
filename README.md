# LR-FHSS Online Range Test

Outdoor range testing for the LR-FHSS backscatter tag. The tag has no GPS, so the phone you carry with it measures the distance.

```
 tag ~~RF~~> Airspy + PC ──ssh push──> GitHub (branch live) ──API──> phone app (GitHub Pages)
                  ▲                                                     │ GPS
                  └─────── git fetch ── GitHub (branch phone) <──API────┘ distance-only track
```

The PC and the phone only ever connect out to GitHub, so this works on networks that block incoming connections and tunnels.

## Parts

| Path | What it does |
|---|---|
| `index.html` | Phone app, served by GitHub Pages. It uses the phone's GPS, has a **Set start here** button that averages 15 s of fixes, shows live packets with distance, SNR and PDR, PDR by distance bin and a track map, beeps on each packet, saves marks and exports CSV. |
| `pc/relay.py` | PC side. It decodes the tag live from the Airspy (pyLR-FHSS `LiveReceiver`), logs locally, force-pushes the latest packets to branch `live`, and pulls the phone's track to log distance per packet. |

## Privacy

Coordinates never leave the phone. The phone uploads only `[time, distance, bearing, accuracy]` relative to a start point it keeps to itself. The CSV export on the phone does contain the full coordinates.

## Setup (once)

1. **Pages:** Settings → Pages → *Deploy from a branch* → `main` / root. The app is then at `https://<user>.github.io/LR-FHSS_Online-Test/`.
2. **Phone token:** Settings → Developer settings → Fine-grained tokens → new token.
   - Repository access: only this repo.
   - Permissions: **Contents: Read and write**.
   - Paste it into the app's *Setup* section. It is stored only in the phone's browser storage.
3. **PC:** needs SSH push access to this repo, `airspy_rx`, and the `lrfhss` package with SoapySDR and SoapyAirspy.

## Range test

On the PC, at the receiver:

```
python pc/relay.py              # --simulate to test without a radio
```

On the phone:

1. Open the Pages URL.
2. Stand at the receiver and tap **Set start here**. Stand still for 15 s while it averages the GPS.
3. Walk with the tag. Each decoded packet beeps (pitch follows SNR) and is placed at your distance at the moment it was on air.
4. Tap **Mark point** at test spots.
5. When done, tap **Download CSV**.

PC logs are in `logs/<run>/`:

- `packets.csv`: every decoded packet.
- `packets_with_distance.csv`: packets merged with the phone track.
- `phone_track.csv`
- `marks.csv`

Packets reach the phone about 10–20 s after they are on air (PC push every ≥ 8 s, phone poll every 4 s). Distance is still exact, because it is looked up at the packet's on-air time, not the time it arrives.
