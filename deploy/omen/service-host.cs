// A GUI-subsystem task root: no terminal is allocated before PowerShell can hide it.
// This process owns the job before spawning anything. Terminating the scheduled action
// therefore tears down PowerShell and every descendant, including venv launcher children.
using System;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Text;

internal static class ElrondServiceHost
{
    private static readonly object LogLock = new object();

    // Windows command-line quoting, including embedded quotes and trailing backslashes.
    private static string Quote(string value)
    {
        StringBuilder result = new StringBuilder("\"");
        int slashes = 0;
        foreach (char character in value)
        {
            if (character == '\\')
                slashes++;
            else
            {
                result.Append('\\', character == '"' ? slashes * 2 + 1 : slashes);
                result.Append(character);
                slashes = 0;
            }
        }
        result.Append('\\', slashes * 2);
        result.Append('"');
        return result.ToString();
    }

    private static void WriteLog(StreamWriter log, string line)
    {
        if (line == null) return;
        try
        {
            lock (LogLock)
                log.WriteLine(DateTime.UtcNow.ToString("o") + " " + line);
        }
        catch (Exception)
        {
            // Reader callbacks run outside Main's catch. Exit deliberately on a failed
            // log volume; closing this process also closes its job and stops the tree.
            Environment.Exit(70);
        }
    }

    [STAThread]
    private static int Main(string[] args)
    {
        if (args.Length < 2 || !Path.IsPathRooted(args[0])) return 64;
        try
        {
            Directory.CreateDirectory(Path.GetDirectoryName(args[0]));
            using (StreamWriter log = new StreamWriter(args[0], true, new UTF8Encoding(false)))
            {
                log.AutoFlush = true;
                IntPtr job = ElrondProcessJob.OwnCurrentProcess();
                ProcessStartInfo start = new ProcessStartInfo();
                start.FileName = Path.Combine(
                    Environment.GetFolderPath(Environment.SpecialFolder.System),
                    @"WindowsPowerShell\v1.0\powershell.exe");
                start.Arguments = String.Join(" ", args.Skip(1).Select(Quote));
                start.UseShellExecute = false;
                start.CreateNoWindow = true;
                start.WindowStyle = ProcessWindowStyle.Hidden;
                start.RedirectStandardOutput = true;
                start.RedirectStandardError = true;
                start.RedirectStandardInput = true;
                using (Process child = new Process())
                {
                    child.StartInfo = start;
                    child.OutputDataReceived += (sender, line) => WriteLog(log, line.Data);
                    child.ErrorDataReceived += (sender, line) => WriteLog(log, line.Data);
                    child.Start();
                    child.StandardInput.Close();
                    child.BeginOutputReadLine();
                    child.BeginErrorReadLine();
                    child.WaitForExit();
                    GC.KeepAlive(job);
                    return child.ExitCode;
                }
            }
        }
        catch (Exception error)
        {
            // Never include argument/configuration values in a launcher exception report.
            try { File.AppendAllText(args[0], "SERVICE_HOST_FAILED " + error.GetType().Name + "\n"); }
            catch (Exception) { }
            return 70;
        }
    }
}
