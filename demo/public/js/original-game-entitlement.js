// Provider integrations are explicit capabilities, never inferred from a KEY file.
export const ENTITLEMENT_PROVIDERS = Object.freeze({
  steam: Object.freeze({
    configured:false, identity:'openid', ownership:'publisher-server',
    documentation:'https://partner.steamgames.com/doc/features/auth',
    unavailable:'Steam ownership verification requires an authorized publisher server.',
  }),
  gog: Object.freeze({
    configured:false, identity:'galaxy-client', ownership:'authorized-game-client',
    documentation:'https://docs.gog.com/galaxyapi/',
    unavailable:'GOG account verification requires an authorized game client integration.',
  }),
});
export function entitlementProvider(name) {
  const provider = ENTITLEMENT_PROVIDERS[name];
  if (!provider) throw new Error('Unknown ownership provider.');
  return provider;
}
export async function verifyEntitlement(name) {
  const provider = entitlementProvider(name);
  // No OAuth token collection, fabricated verification, or public asset fallback.
  throw new Error(provider.unavailable);
}
