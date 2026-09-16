class Dropin < Formula
  desc "Folderless, Spotlight-aware encrypted drop archiver"
  homepage "https://github.com/jmller/dropin"
  url "https://github.com/jmller/dropin/releases/download/v0.1.0/dropin-0.1.0.pyz", using: :nounzip
  sha256 "f9cf7dbe9eed674f4d1a49c6ac23182b3dc94af7d1308c373b9e128efc71d21d"
  license :cannot_represent

  depends_on arch: :arm64
  depends_on "python@3.13"
  depends_on "restic"
  depends_on "rclone"

  def install
    libexec.install cached_download => "dropin.pyz"
    (libexec/"dropin.pyz").chmod 0755
    (bin/"dropin").write_env_script libexec/"dropin.pyz",
      PATH: "#{Formula["python@3.13"].opt_libexec}/bin:$PATH"
  end

  test do
    assert_equal "dropin 0.1.0", shell_output("#{bin}/dropin --version").strip
  end
end
