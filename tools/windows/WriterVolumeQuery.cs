// Standalone boot helper: no Python, project imports, user config or commands.
using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.IO;
using System.Runtime.InteropServices;
using System.Security.AccessControl;
using System.Security.Principal;
using System.Web.Script.Serialization;

namespace GengAgent.WriterBoundary {
    public static class VolumeQuery {
        [StructLayout(LayoutKind.Sequential)] struct UnicodeString {
            public ushort Length, MaximumLength;
            public IntPtr Buffer;
        }
        [StructLayout(LayoutKind.Sequential)] struct ObjectAttributes {
            public int Length;
            public IntPtr RootDirectory, ObjectName;
            public uint Attributes;
            public IntPtr SecurityDescriptor, SecurityQualityOfService;
        }
        [DllImport("kernelbase.dll", CharSet=CharSet.Unicode)] static extern int DeriveCapabilitySidsFromName(string name, out IntPtr groups, out uint groupCount, out IntPtr capabilities, out uint count);
        [DllImport("kernel32.dll")] static extern IntPtr LocalFree(IntPtr pointer);
        [DllImport("advapi32.dll")] static extern IntPtr FreeSid(IntPtr pointer);
        [DllImport("kernel32.dll")] static extern bool CloseHandle(IntPtr handle);
        [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)] static extern IntPtr CreateFile(string name, uint access, uint share, IntPtr security, uint creation, uint flags, IntPtr template);
        [DllImport("ntdll.dll")] static extern int NtOpenDirectoryObject(out IntPtr handle, uint access, ref ObjectAttributes attributes);
        [DllImport("ntdll.dll")] static extern int NtOpenSymbolicLinkObject(out IntPtr handle, uint access, ref ObjectAttributes attributes);
        [DllImport("advapi32.dll")] static extern uint GetSecurityInfo(IntPtr handle, int kind, uint info, IntPtr owner, IntPtr group, out IntPtr dacl, IntPtr sacl, out IntPtr descriptor);
        [DllImport("advapi32.dll")] static extern uint GetSecurityDescriptorLength(IntPtr descriptor);
        [DllImport("advapi32.dll")] static extern uint SetSecurityInfo(IntPtr handle, int kind, uint info, IntPtr owner, IntPtr group, byte[] dacl, IntPtr sacl);

        static void Check(uint error) { if (error != 0) throw new Win32Exception((int)error); }
        public static SecurityIdentifier Capability() {
            IntPtr groups, capabilities;
            uint groupCount, count;
            int result = DeriveCapabilitySidsFromName("GengAgent.TrustedRuntime.Read.v1", out groups, out groupCount, out capabilities, out count);
            if (result < 0) Marshal.ThrowExceptionForHR(result);
            try {
                if (count != 1) throw new InvalidOperationException("Unexpected project capability count");
                return new SecurityIdentifier(Marshal.ReadIntPtr(capabilities));
            } finally {
                foreach (var pair in new[] { Tuple.Create(groups, groupCount), Tuple.Create(capabilities, count) }) {
                    for (int index=0; index<pair.Item2; index++) FreeSid(Marshal.ReadIntPtr(pair.Item1, index * IntPtr.Size));
                    LocalFree(pair.Item1);
                }
            }
        }
        static IntPtr Open(string name, int kind, bool write) {
            uint access = 0x20000u | (write ? 0x40000u : 0u);
            if (kind == 2) {
                IntPtr file = CreateFile(name, access, 3, IntPtr.Zero, 3, 0, IntPtr.Zero);
                if (file == new IntPtr(-1)) throw new Win32Exception(Marshal.GetLastWin32Error());
                return file;
            }
            IntPtr text = Marshal.StringToHGlobalUni(name);
            IntPtr unicode = Marshal.AllocHGlobal(Marshal.SizeOf(typeof(UnicodeString)));
            try {
                Marshal.StructureToPtr(new UnicodeString {Length=(ushort)(name.Length*2), MaximumLength=(ushort)(name.Length*2+2), Buffer=text}, unicode, false);
                var attributes = new ObjectAttributes {Length=Marshal.SizeOf(typeof(ObjectAttributes)), ObjectName=unicode, Attributes=0x40};
                IntPtr handle;
                int status = kind == 0 ? NtOpenDirectoryObject(out handle, access, ref attributes) : NtOpenSymbolicLinkObject(out handle, access, ref attributes);
                if (status < 0) throw new InvalidOperationException(name + ": NTSTATUS " + status.ToString("X8"));
                return handle;
            } finally { Marshal.FreeHGlobal(unicode); Marshal.FreeHGlobal(text); }
        }
        static RawSecurityDescriptor Read(IntPtr handle) {
            IntPtr dacl, descriptor;
            Check(GetSecurityInfo(handle, 6, 4, IntPtr.Zero, IntPtr.Zero, out dacl, IntPtr.Zero, out descriptor));
            try {
                var bytes = new byte[GetSecurityDescriptorLength(descriptor)];
                Marshal.Copy(descriptor, bytes, 0, bytes.Length);
                return new RawSecurityDescriptor(bytes, 0);
            } finally { LocalFree(descriptor); }
        }
        // Add just the missing read ACE. Preserve every existing ACE, including
        // other principals, their order, object ACEs and inheritance flags.
        public static RawAcl WithReadGrant(RawAcl original, SecurityIdentifier sid, int mask) {
            if (original == null) throw new InvalidOperationException("Refusing to replace an unrestricted ACL");
            var bytes = new byte[original.BinaryLength]; original.GetBinaryForm(bytes, 0);
            var result = new RawAcl(bytes, 0);
            int allowed = 0;
            foreach (GenericAce item in original) {
                var ace = item as QualifiedAce;
                if (ace == null || !ace.SecurityIdentifier.Equals(sid) || (ace.AceFlags & AceFlags.InheritOnly) != 0) continue;
                if (ace.AceQualifier == AceQualifier.AccessDenied && (ace.AccessMask & mask) != 0)
                    throw new InvalidOperationException("An existing project capability deny requires administrator review");
                if (ace.AceQualifier == AceQualifier.AccessAllowed && !ace.IsCallback) allowed |= ace.AccessMask;
            }
            int missing = mask & ~allowed;
            if (missing != 0) {
                int position = 0;
                while (position < result.Count && (result[position].AceFlags & AceFlags.Inherited) == 0) position++;
                result.InsertAce(position, new CommonAce(AceFlags.None, AceQualifier.AccessAllowed, missing, sid, false, null));
            }
            return result;
        }
        static List<object> Inspect(bool apply) {
            SecurityIdentifier sid = Capability();
            // Fixed compatibility operations only. No caller-selected objects,
            // paths, rights, credentials, process handles or shell commands.
            string[] names = {@"\GLOBAL??", @"\GLOBAL??\C:", @"\GLOBAL??\D:", @"\GLOBAL??\MountPointManager", @"\\.\MountPointManager"};
            int[] kinds = {0, 1, 1, 1, 2};
            int[] masks = {0x20003, 0x20001, 0x20001, 0x20001, 0x120089};
            var results = new List<object>();
            for (int index=0; index<names.Length; index++) {
                IntPtr handle = Open(names[index], kinds[index], apply);
                try {
                    var before = Read(handle);
                    RawAcl next = WithReadGrant(before.DiscretionaryAcl, sid, masks[index]);
                    bool changed = next.BinaryLength != before.DiscretionaryAcl.BinaryLength;
                    if (apply && changed) {
                        byte[] bytes = new byte[next.BinaryLength]; next.GetBinaryForm(bytes, 0);
                        Check(SetSecurityInfo(handle, 6, 4, IntPtr.Zero, IntPtr.Zero, bytes, IntPtr.Zero));
                    }
                    var after = Read(handle);
                    bool ready = WithReadGrant(after.DiscretionaryAcl, sid, masks[index]).BinaryLength == after.DiscretionaryAcl.BinaryLength;
                    results.Add(new {name=names[index], mask=masks[index], ready=ready, changed=apply && changed,
                        before_sddl=before.GetSddlForm(AccessControlSections.Access), after_sddl=after.GetSddlForm(AccessControlSections.Access)});
                    if (apply && !ready) throw new InvalidOperationException("Permission verification failed for " + names[index]);
                } finally { CloseHandle(handle); }
            }
            return results;
        }
        public static List<object> CheckOnly() { return Inspect(false); }
        public static int Main(string[] args) {
            if (args.Length != 0) return 2;
            var record = new Dictionary<string, object> {
                {"time", DateTimeOffset.Now.ToString("o")}, {"success", false},
                {"identity_sid", WindowsIdentity.GetCurrent().User.Value}, {"capability_sid", Capability().Value}
            };
            int exitCode = 1;
            try { record["objects"] = Inspect(true); record["success"] = true; exitCode = 0; }
            catch (Exception error) { record["error"] = error.ToString(); }
            // The executable and logs share an administrator-protected directory.
            // Never write as SYSTEM to a user-controlled report or temp path.
            string directory = Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "logs");
            Directory.CreateDirectory(directory);
            string json = new JavaScriptSerializer().Serialize(record);
            File.WriteAllText(Path.Combine(directory, "run-" + DateTime.UtcNow.ToString("yyyyMMdd-HHmmss") + "-" + Guid.NewGuid().ToString("N") + ".json"), json);
            string pending = Path.Combine(directory, "latest-" + Guid.NewGuid().ToString("N") + ".tmp");
            string latest = Path.Combine(directory, "latest.json");
            File.WriteAllText(pending, json);
            if (File.Exists(latest)) File.Replace(pending, latest, null);
            else File.Move(pending, latest);
            return exitCode;
        }
    }
}
