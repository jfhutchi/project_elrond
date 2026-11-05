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
    private const long LogLimitBytes = 32L * 1024 * 1024;

    // Both redirected streams share this writer and WriteLog's callback lock. Keep
    // three older files; these paths belong only to this host's configured log.
    private sealed class RotatingLog : TextWriter
    {
        private readonly string path;
        private readonly Encoding encoding = new UTF8Encoding(false);
        private FileStream stream;
        public override Encoding Encoding { get { return encoding; } }

        public RotatingLog(string path)
        {
            this.path = path;
            for (int archive = 1; archive <= 3; archive++)
                TrimOversized(path + "." + archive);
            if (File.Exists(path) && new FileInfo(path).Length >= LogLimitBytes)
            {
                TrimOversized(path);
                Rotate();
            }
            Open();
        }

        private static void TrimOversized(string name)
        {
            if (!File.Exists(name) || new FileInfo(name).Length <= LogLimitBytes) return;
            string temporary = name + ".retention.tmp";
            bool created = false;
            try
            {
                using (FileStream input = File.OpenRead(name))
                using (FileStream output = new FileStream(temporary, FileMode.Create,
                                                          FileAccess.Write, FileShare.None))
                {
                    created = true;
                    long tail = input.Length - LogLimitBytes;
                    input.Position = tail;
                    // Prefer retaining complete recent records. If one record alone
                    // exceeds the cap, retain its suffix on a UTF-8 character boundary.
                    while (input.Position < input.Length && input.ReadByte() != '\n') { }
                    if (input.Position == input.Length)
                    {
                        input.Position = tail;
                        int value;
                        while ((value = input.ReadByte()) >= 0 && (value & 0xC0) == 0x80) { }
                        if (value >= 0) input.Position--;
                    }
                    input.CopyTo(output);
                    output.Flush(true);
                }
                File.Replace(temporary, name, null);
            }
            finally
            {
                if (created && File.Exists(temporary)) File.Delete(temporary);
            }
        }

        private void Open()
        {
            stream = new FileStream(path, FileMode.Append, FileAccess.Write, FileShare.Read);
        }

        private void Rotate()
        {
            if (File.Exists(path + ".3")) File.Delete(path + ".3");
            for (int archive = 2; archive >= 1; archive--)
                if (File.Exists(path + "." + archive))
                    File.Move(path + "." + archive, path + "." + (archive + 1));
            if (File.Exists(path)) File.Move(path, path + ".1");
        }

        public override void WriteLine(string line)
        {
            byte[] bytes = encoding.GetBytes(line + NewLine);
            int offset = 0;
            while (offset < bytes.Length)
            {
                // Keep ordinary records intact. An exceptionally oversized record
                // spans files without changing its bytes or splitting UTF-8 characters.
                if (stream.Length > 0 && stream.Length + bytes.Length - offset > LogLimitBytes)
                {
                    stream.Dispose();
                    Rotate();
                    Open();
                }
                int count = (int)Math.Min(LogLimitBytes - stream.Length, bytes.Length - offset);
                while (offset + count < bytes.Length && (bytes[offset + count] & 0xC0) == 0x80)
                    count--;
                stream.Write(bytes, offset, count);
                stream.Flush();
                offset += count;
            }
        }

        protected override void Dispose(bool disposing)
        {
            if (disposing && stream != null) stream.Dispose();
            base.Dispose(disposing);
        }
    }

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

    private static void WriteLog(TextWriter log, string line)
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
            using (TextWriter log = new RotatingLog(args[0]))
            {
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
            try
            {
                using (TextWriter log = new RotatingLog(args[0]))
                    log.WriteLine("SERVICE_HOST_FAILED " + error.GetType().Name);
            }
            catch (Exception) { }
            return 70;
        }
    }
}
