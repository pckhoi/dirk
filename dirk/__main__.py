from dirk.commands import get_parser

parser = get_parser()

args = parser.parse_args()
args.exec(None, args)
