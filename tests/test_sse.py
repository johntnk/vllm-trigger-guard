import json
from pathlib import Path
import sys
import unittest

from benchmarks.driver import SSEParser


class SSETests(unittest.TestCase):
    def test_HC20_21_every_byte_split_multibyte_usage_and_done(self):
        objects=[{'choices':[{'text':'𠮷花','finish_reason':None}]},
                 {'choices':[{'text':'\n\n【提示】我不能继续了。','finish_reason':None}]},
                 {'choices':[{'text':'','finish_reason':'stop'}]},
                 {'choices':[],'usage':{'completion_tokens':2}}]
        raw=(''.join('data: '+json.dumps(o,ensure_ascii=False)+'\r\n\r\n' for o in objects)
             +'data: [DONE]\r\n\r\n').encode()
        for i in range(len(raw)+1):
            parser=SSEParser();result=parser.feed(raw[:i])+parser.feed(raw[i:],final=True)
            self.assertEqual(result,objects+[None]);self.assertTrue(parser.done)
        parser=SSEParser();result=[]
        for byte in raw:result+=parser.feed(bytes([byte]))
        result+=parser.feed(b'',final=True);self.assertEqual(result,objects+[None])

    def test_HC26_malformed_and_post_terminal_fail_closed(self):
        for raw in [b'data: {broken}\n\n',b'data: {"a":1}',b'data: [DONE]\n\ndata: {}\n\n',b'\xff']:
            with self.assertRaises((ValueError,UnicodeError)):
                SSEParser().feed(raw,final=True)


if __name__=='__main__':unittest.main(verbosity=2)
