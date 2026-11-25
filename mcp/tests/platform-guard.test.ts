/**
 * Intel Macs (darwin-x64) have no onnxruntime-node binary. The guard must exit
 * early with a clear message there, and stay silent everywhere else.
 */
import { assertSupportedPlatform, isUnsupportedPlatform, INTEL_MAC_MESSAGE } from '../src/platform-guard';

describe('platform guard', () => {
  it('exits 1 with a clear, synchronously written message on darwin-x64', () => {
    const exit = jest.fn();
    const write = jest.fn();
    assertSupportedPlatform('darwin', 'x64', exit, write);
    expect(exit).toHaveBeenCalledWith(1);
    expect(write).toHaveBeenCalledWith(INTEL_MAC_MESSAGE + '\n');
    expect(INTEL_MAC_MESSAGE).toMatch(/Intel Macs/);
    expect(INTEL_MAC_MESSAGE).toMatch(/not supported/);
    expect(INTEL_MAC_MESSAGE).toMatch(/arm64/);
    expect(INTEL_MAC_MESSAGE).toMatch(/tree\/main\/mcp/);
  });

  it('still exits if the stderr write throws', () => {
    const exit = jest.fn();
    assertSupportedPlatform('darwin', 'x64', exit, () => { throw new Error('EPIPE'); });
    expect(exit).toHaveBeenCalledWith(1);
  });

  it.each([['freebsd', 'x64'], ['linux', 'ia32'], ['win32', 'ia32'], ['darwin', 'ia32']])(
    'exits on unlisted %s-%s with a generic message', (platform, arch) => {
      const exit = jest.fn();
      const write = jest.fn();
      assertSupportedPlatform(platform, arch, exit, write);
      expect(exit).toHaveBeenCalledWith(1);
      expect(write.mock.calls[0][0]).toContain(`${platform}-${arch} is not supported`);
    });

  it.each([['darwin', 'arm64'], ['linux', 'x64'], ['linux', 'arm64'], ['win32', 'x64'], ['win32', 'arm64']])(
    'does nothing on %s-%s', (platform, arch) => {
      const exit = jest.fn();
      const write = jest.fn();
      assertSupportedPlatform(platform, arch, exit, write);
      expect(exit).not.toHaveBeenCalled();
      expect(write).not.toHaveBeenCalled();
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
