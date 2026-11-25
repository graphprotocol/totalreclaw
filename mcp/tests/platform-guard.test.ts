/**
 * Intel Macs (darwin-x64) have no onnxruntime-node binary. The guard must exit
 * early with a clear message there, and stay silent everywhere else.
 */
import { assertSupportedPlatform, isUnsupportedPlatform, INTEL_MAC_MESSAGE } from '../src/platform-guard';

describe('platform guard', () => {
  let errSpy: jest.SpyInstance;
  beforeEach(() => { errSpy = jest.spyOn(console, 'error').mockImplementation(() => {}); });
  afterEach(() => { errSpy.mockRestore(); });

  it('exits 1 with a clear message on darwin-x64', () => {
    const exit = jest.fn();
    assertSupportedPlatform('darwin', 'x64', exit);
    expect(exit).toHaveBeenCalledWith(1);
    expect(errSpy).toHaveBeenCalledWith(INTEL_MAC_MESSAGE);
    expect(INTEL_MAC_MESSAGE).toMatch(/Intel Macs/);
    expect(INTEL_MAC_MESSAGE).toMatch(/not supported/);
    expect(INTEL_MAC_MESSAGE).toMatch(/tree\/main\/mcp/);
  });

  it.each([['darwin', 'arm64'], ['linux', 'x64'], ['linux', 'arm64'], ['win32', 'x64']])(
    'does nothing on %s-%s', (platform, arch) => {
      const exit = jest.fn();
      assertSupportedPlatform(platform, arch, exit);
      expect(exit).not.toHaveBeenCalled();
      expect(errSpy).not.toHaveBeenCalled();
    });

  it('defaults to process.platform / process.arch (stubbed)', () => {
    const plat = Object.getOwnPropertyDescriptor(process, 'platform')!;
    const arch = Object.getOwnPropertyDescriptor(process, 'arch')!;
    try {
      Object.defineProperty(process, 'platform', { value: 'darwin' });
      Object.defineProperty(process, 'arch', { value: 'x64' });
      expect(isUnsupportedPlatform()).toBe(true);
      Object.defineProperty(process, 'arch', { value: 'arm64' });
      expect(isUnsupportedPlatform()).toBe(false);
    } finally {
      Object.defineProperty(process, 'platform', plat);
      Object.defineProperty(process, 'arch', arch);
    }
  });
});
