/** Provider icon: intranet deployment has no CDN access, so we always
 *  return an empty URL — consumers (ProviderIcon / letter-avatar fallbacks)
 *  render a local first-letter avatar instead. */
export const providerIcon = (_provider: string) => {
  void _provider;
  return "";
};
