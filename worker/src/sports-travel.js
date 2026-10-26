// Public-area origin only. Never use a person's address or geo.js city-distance estimate.
export const TRAVEL_ORIGIN = 'Westminster north, CO 80234';
export function travelBucket(minutes) {
  if (!Number.isFinite(minutes) || minutes < 0) return 'unknown';
  return minutes <= 30 ? '0-30' : minutes <= 60 ? '30-60' : minutes <= 90 ? '60-90' : '90+';
}
export function venueTravel(event) {
  const route = event.venue_route;
  const unknown = { drive_minutes: null, drive_km: null, drive_bucket: 'unknown', drive_source: '', drive_checked_at: '', drive_origin: TRAVEL_ORIGIN };
  // Collector contract: precomputed road route tied to the actual venue, with provenance.
  // No network geocoding, paid map API, or straight-line-to-driving conversion here.
  if (!route || route.origin !== TRAVEL_ORIGIN || route.mode !== 'driving' || route.venue !== (event.venue || event.location) || route.city !== (event.city || '') || !route.venue || !/^https:\/\//.test(route.source_url || '') || !Number.isFinite(Date.parse(route.checked_at)) || !Number.isFinite(route.minutes) || route.minutes < 0 || !Number.isFinite(route.km) || route.km < 0) return unknown;
  return { drive_minutes: route.minutes, drive_km: route.km, drive_bucket: travelBucket(route.minutes), drive_source: route.source_url, drive_checked_at: route.checked_at, drive_origin: TRAVEL_ORIGIN };
}
