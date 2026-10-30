using System;
using System.Diagnostics;
using System.IO;
using System.Reflection;
using System.Windows.Forms;

[assembly: AssemblyTitle("Citadel")]
[assembly: AssemblyVersion("0.4.0.0")]
[assembly: AssemblyFileVersion("0.4.0.0")]

internal static class Launcher
{
    [STAThread]
    private static int Main(string[] args)
    {
        try
        {
            string root = Path.GetDirectoryName(Assembly.GetExecutingAssembly().Location);
            bool check = Array.IndexOf(args, "--check") >= 0;
            string python = Path.Combine(root, "runtime", check ? "python.exe" : "pythonw.exe");
            if (!File.Exists(python)) throw new FileNotFoundException("Missing bundled runtime. Reinstall Citadel.", python);
            ProcessStartInfo info = new ProcessStartInfo(python, "-B -X utf8 -m assistant.launcher" + (check ? " --check" : ""));
            info.WorkingDirectory = root;
            info.UseShellExecute = false;
            info.CreateNoWindow = true;
            info.EnvironmentVariables.Remove("PYTHONHOME");
            info.EnvironmentVariables.Remove("PYTHONPATH");
            info.EnvironmentVariables["PYTHONUTF8"] = "1";
            info.EnvironmentVariables["PYTHONDONTWRITEBYTECODE"] = "1";
            using (Process child = Process.Start(info))
            {
                if (check)
                {
                    if (!child.WaitForExit(60000)) { child.Kill(); return 2; }
                    return child.ExitCode;
                }
            }
            return 0;
        }
        catch (Exception error)
        {
            MessageBox.Show(error.Message, "Citadel startup error", MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 1;
        }
    }
}
